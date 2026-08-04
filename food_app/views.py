from .models import UserProfile
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from .models import Product, CartItem, Category, Cart, DeliveryAddress, Order, Delivery, DeliveryPerson, OrderItem
from django.contrib.auth.models import User
from django.http import HttpResponse, JsonResponse
from django.core.serializers.json import DjangoJSONEncoder
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import ensure_csrf_cookie
from django.utils import timezone
from django.db.models import Sum
from .forms import DeliveryAddressForm


def generate_delivery_code():
    from secrets import randbelow

    return "".join(str(randbelow(10)) for _ in range(6))


def get_user_role(user):
    try:
        return user.userprofile.role
    except UserProfile.DoesNotExist:
        return "customer"


# Keeps Order.status (shown to customers/admins) in sync with Delivery.status
DELIVERY_TO_ORDER_STATUS = {
    "pending": "Pending",
    "assigned": "Assigned",
    "picked_up": "Out for Delivery",
    "in_transit": "Out for Delivery",
    "delivered": "Delivered",
    "cancelled": "Cancelled",
}


def sync_order_status(delivery):
    """Update the parent Order.status to reflect the current Delivery.status."""
    new_status = DELIVERY_TO_ORDER_STATUS.get(delivery.status, delivery.status)
    if delivery.order.status != new_status:
        delivery.order.status = new_status
        delivery.order.save(update_fields=["status"])


def redirect_for_role(request):
    if request.user.is_authenticated:
        role = get_user_role(request.user)
        if role == "admin":
            messages.info(request, "Admin staff use the admin dashboard.")
            return redirect("admin_dashboard")
        if role == "delivery":
            messages.info(request, "Delivery staff use the delivery dashboard.")
            return redirect("delivery_dashboard")
    return None


@login_required(login_url="login")
def place_order(request):
    cart_obj = get_object_or_404(Cart, user=request.user, active=True)
    cart_items = cart_obj.items.select_related('product').all()

    if not cart_items:
        messages.error(request, "Your cart is empty.")
        return redirect("cart")

    # Get delivery address
    delivery_address_id = request.POST.get("delivery_address")
    if not delivery_address_id:
        messages.error(request, "Please select a delivery address.")
        return redirect("checkout")
    
    try:
        delivery_address = DeliveryAddress.objects.get(id=delivery_address_id, user=request.user)
    except DeliveryAddress.DoesNotExist:
        messages.error(request, "Invalid delivery address.")
        return redirect("checkout")

    # Calculate total price
    total_price = cart_obj.total_price

    # Create Order
    order = Order.objects.create(
        user=request.user,
        cart=cart_obj,
        total=total_price,
        delivery_address=delivery_address
    )

    # Create OrderItems from CartItems
    for cart_item in cart_items:
        OrderItem.objects.create(
            order=order,
            product=cart_item.product,
            quantity=cart_item.quantity,
            price=cart_item.product.price
        )

    # Create Delivery record
    from datetime import timedelta
    estimated_time = timezone.now() + timedelta(hours=1)  # 1 hour estimated delivery
    
    delivery = Delivery.objects.create(
        order=order,
        status='pending',
        estimated_delivery_time=estimated_time,
        delivery_code=generate_delivery_code(),
    )

    # ---- Clear cart after placing order ----
    cart_obj.items.all().delete()
    cart_obj.active = False
    cart_obj.save()

    messages.success(request, "Order placed successfully! You can track your delivery now.")

    return redirect("order_detail", order_id=order.id)

def home(request):
    redirect_response = redirect_for_role(request)
    if redirect_response:
        return redirect_response

    categories = Category.objects.all() if hasattr(Category, 'objects') else []
    featured = Product.objects.filter(available=True)[:8] if hasattr(Product, 'objects') else []
    return render(request, "food_app/index.html", {"categories": categories, "products": featured})

def product_list(request):
    redirect_response = redirect_for_role(request)
    if redirect_response:
        return redirect_response

    # show all products grouped by category
    categories = Category.objects.prefetch_related('products').all()
    return render(request, "food_app/menu.html", {"categories": categories})

def category_products(request, slug):
    redirect_response = redirect_for_role(request)
    if redirect_response:
        return redirect_response

    category = get_object_or_404(Category, slug=slug)
    products = category.products.filter(available=True)
    return render(request, "food_app/categories.html", {"category": category, "products": products})

def product_detail(request, product_id):
    redirect_response = redirect_for_role(request)
    if redirect_response:
        return redirect_response

    product = get_object_or_404(Product, pk=product_id)
    return render(request, "food_app/product_detail.html", {"product": product})

@login_required(login_url="login")
def cart(request):
    redirect_response = redirect_for_role(request)
    if redirect_response:
        return redirect_response

    # find active cart for user or create one
    cart_obj, created = Cart.objects.get_or_create(user=request.user, active=True)
    cart_items = cart_obj.items.select_related('product').all()
    total_price = cart_obj.total_price if cart_obj else 0
    return render(request, "food_app/cart.html", {
        "cart_items": cart_items,
        "total_price": total_price
    })

def add_to_cart(request, product_id):
    if not request.user.is_authenticated:
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({
                "success": False,
                "message": "Please log in to add items to your cart.",
                "require_login": True,
            }, status=401)
        return redirect("login")

    product = get_object_or_404(Product, pk=product_id)
    cart_obj, created = Cart.objects.get_or_create(user=request.user, active=True)
    cart_item, created = CartItem.objects.get_or_create(cart=cart_obj, product=product)
    if not created:
        cart_item.quantity += 1
    cart_item.save()
    messages.success(request, "Item added to cart!")

    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse({
            "success": True,
            "message": "Item added to cart!",
            "item_count": cart_obj.items.count(),
        })

    return redirect(request.META.get("HTTP_REFERER", "menu"))


def remove_from_cart(request, product_id):
    if not request.user.is_authenticated:
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({
                "success": False,
                "message": "Please log in to manage your cart.",
                "require_login": True,
            }, status=401)
        return redirect("login")

    cart_obj = get_object_or_404(Cart, user=request.user, active=True)
    item = get_object_or_404(CartItem, cart=cart_obj, product_id=product_id)
    item.delete()
    messages.success(request, "Item removed!")

    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse({
            "success": True,
            "message": "Item removed!",
            "item_count": cart_obj.items.count(),
        })

    return redirect("cart")

@login_required(login_url="login")
def checkout(request):
    redirect_response = redirect_for_role(request)
    if redirect_response:
        return redirect_response

    cart_obj = get_object_or_404(Cart, user=request.user, active=True)
    cart_items = cart_obj.items.select_related('product').all()
    total_price = cart_obj.total_price
    delivery_addresses = request.user.delivery_addresses.all()
    default_address = delivery_addresses.filter(is_default=True).first()
    
    return render(request, "food_app/checkout.html", {
        "cart_items": cart_items,
        "total_price": total_price,
        "delivery_addresses": delivery_addresses,
        "default_address": default_address
    })

@ensure_csrf_cookie
def react_app(request):
    return render(request, "food_app/react_app.html")


def api_products(request):
    products = Product.objects.filter(available=True).select_related("category").order_by("name")
    payload = {
        "count": products.count(),
        "products": [
            {
                "id": product.id,
                "name": product.name,
                "description": product.description,
                "price": float(product.price),
                "category": product.category.name,
                "image": product.image.url if product.image else None,
            }
            for product in products
        ],
    }
    return JsonResponse(payload)


@login_required(login_url="login")
def api_delivery_addresses(request):
    redirect_response = redirect_for_role(request)
    if redirect_response:
        return redirect_response

    addresses = request.user.delivery_addresses.all().order_by("-is_default", "id")
    payload = {
        "count": addresses.count(),
        "addresses": [
            {
                "id": address.id,
                "street_address": address.street_address,
                "city": address.city,
                "postal_code": address.postal_code,
                "phone": address.phone,
                "is_default": address.is_default,
            }
            for address in addresses
        ],
    }
    return JsonResponse(payload)


@login_required(login_url="login")
def api_orders(request):
    redirect_response = redirect_for_role(request)
    if redirect_response:
        return redirect_response

    orders = Order.objects.filter(user=request.user).select_related("delivery_address").prefetch_related("items__product").order_by("-created_at")
    payload = {
        "count": orders.count(),
        "orders": [
            {
                "id": order.id,
                "status": order.status,
                "total": float(order.total),
                "created_at": order.created_at.isoformat(),
                "delivery_address": order.delivery_address.street_address if order.delivery_address else None,
                "delivery_verification_code": (
                    order.delivery.delivery_code
                    if hasattr(order, "delivery") and order.delivery and order.delivery.status != "delivered"
                    else None
                ),
                "items": [
                    {
                        "name": item.product.name if item.product else "Product",
                        "quantity": item.quantity,
                    }
                    for item in order.items.all()
                ],
            }
            for order in orders
        ],
    }
    return JsonResponse(payload)


def user_login(request):
    if request.method == "POST":
        username = request.POST.get("username")
        password = request.POST.get("password")

        user = authenticate(request, username=username, password=password)

        if user:
            login(request, user)

            try:
                role = user.userprofile.role
            except UserProfile.DoesNotExist:
                role = "customer"

            messages.success(request, "Login successful!")

            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                redirect_to = "/admin-dashboard/" if role == "admin" else "/delivery-dashboard/" if role == "delivery" else "/"
                return JsonResponse({"success": True, "redirect_to": redirect_to})

            if role == "admin":
                return redirect("admin_dashboard")

            elif role == "delivery":
                return redirect("delivery_dashboard")

            else:
                return redirect("home")

        else:
            messages.error(request, "Invalid username or password")
            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                return JsonResponse({"success": False, "message": "Invalid username or password"}, status=401)

    return render(request, "food_app/login.html")

def user_logout(request):
    logout(request)
    messages.success(request, "Logged out successfully!")
    return redirect("home")

def user_register(request):
    if request.method == "POST":
        username = request.POST.get("username")
        email = request.POST.get("email")
        password = request.POST.get("password")
        is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"

        if not username or not password:
            messages.error(request, "Username and password are required!")
            if is_ajax:
                return JsonResponse({"success": False, "message": "Username and password are required!"}, status=400)
            return redirect("register")

        if User.objects.filter(username=username).exists():
            messages.error(request, "Username already exists!")
            if is_ajax:
                return JsonResponse({"success": False, "message": "Username already exists!"}, status=400)
            return redirect("register")

        user = User.objects.create_user(
            username=username,
            email=email,
            password=password
        )

        login(request, user)
        messages.success(request, "Registration successful! Welcome aboard.")

        if is_ajax:
            return JsonResponse({"success": True, "redirect_to": "/"})

        return redirect("home")

    return render(request, "food_app/register.html")
# ============ DELIVERY SYSTEM VIEWS ============

@login_required(login_url="login")
def user_dashboard(request):
    """Show a personalized dashboard for the authenticated customer."""
    redirect_response = redirect_for_role(request)
    if redirect_response:
        return redirect_response

    orders = request.user.order_set.select_related("delivery_address").prefetch_related("items__product").order_by("-created_at")[:5]
    addresses = request.user.delivery_addresses.all().order_by("-is_default", "id")
    cart_obj = Cart.objects.filter(user=request.user, active=True).first()
    cart_items = cart_obj.items.select_related("product").all() if cart_obj else []
    total_orders = request.user.order_set.count()
    total_spent = request.user.order_set.aggregate(total_spent=Sum("total"))["total_spent"] or 0

    return render(request, "food_app/user_dashboard.html", {
        "orders": orders,
        "addresses": addresses,
        "cart_items": cart_items,
        "total_orders": total_orders,
        "total_spent": total_spent,
    })


@login_required(login_url="login")
def delivery_addresses(request):
    """List user's delivery addresses"""
    redirect_response = redirect_for_role(request)
    if redirect_response:
        return redirect_response

    addresses = request.user.delivery_addresses.all()
    return render(request, "food_app/delivery_addresses.html", {"addresses": addresses})


@login_required(login_url="login")
def add_delivery_address(request):
    """Add new delivery address"""
    redirect_response = redirect_for_role(request)
    if redirect_response:
        return redirect_response

    if request.method == "POST":
        form = DeliveryAddressForm(request.POST)
        if form.is_valid():
            address = form.save(commit=False)
            address.user = request.user

            # If marking as default, unmark other addresses
            if address.is_default:
                DeliveryAddress.objects.filter(user=request.user, is_default=True).update(is_default=False)

            address.save()
            messages.success(request, "Delivery address added successfully!")

            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                return JsonResponse({
                    "success": True,
                    "message": "Delivery address added successfully!",
                    "address_id": address.id,
                })

            return redirect("delivery_addresses")

        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({
                "success": False,
                "errors": form.errors,
            }, status=400)
    else:
        form = DeliveryAddressForm()

    return render(request, "food_app/add_delivery_address.html", {"form": form})


@login_required(login_url="login")
def edit_delivery_address(request, address_id):
    """Edit delivery address"""
    redirect_response = redirect_for_role(request)
    if redirect_response:
        return redirect_response

    address = get_object_or_404(DeliveryAddress, id=address_id, user=request.user)
    
    if request.method == "POST":
        form = DeliveryAddressForm(request.POST, instance=address)
        if form.is_valid():
            updated_address = form.save(commit=False)
            
            if updated_address.is_default:
                DeliveryAddress.objects.filter(user=request.user, is_default=True).exclude(id=address.id).update(is_default=False)
            
            updated_address.save()
            messages.success(request, "Delivery address updated successfully!")
            return redirect("delivery_addresses")
    else:
        form = DeliveryAddressForm(instance=address)
    
    return render(request, "food_app/edit_delivery_address.html", {"form": form, "address": address})


@login_required(login_url="login")
def delete_delivery_address(request, address_id):
    """Delete delivery address"""
    redirect_response = redirect_for_role(request)
    if redirect_response:
        return redirect_response

    address = get_object_or_404(DeliveryAddress, id=address_id, user=request.user)
    address.delete()
    messages.success(request, "Delivery address deleted!")
    return redirect("delivery_addresses")


@login_required(login_url="login")
def order_detail(request, order_id):
    """View order details and delivery tracking"""
    redirect_response = redirect_for_role(request)
    if redirect_response:
        return redirect_response

    order = get_object_or_404(Order, id=order_id, user=request.user)
    order_items = order.items.all()
    delivery = getattr(order, 'delivery', None)
    
    return render(request, "food_app/order_detail.html", {
        "order": order,
        "order_items": order_items,
        "delivery": delivery
    })


@login_required(login_url="login")
def order_status(request, order_id):
    """View order status for a user's order."""
    redirect_response = redirect_for_role(request)
    if redirect_response:
        return redirect_response

    order = get_object_or_404(Order, id=order_id, user=request.user)
    delivery = getattr(order, 'delivery', None)
    order_items = order.items.all()

    return render(request, "food_app/order_status.html", {
        "order": order,
        "delivery": delivery,
        "order_items": order_items,
    })


@login_required(login_url="login")
def order_history(request):
    """View user's order history"""
    redirect_response = redirect_for_role(request)
    if redirect_response:
        return redirect_response

    orders = request.user.order_set.all().order_by('-created_at')
    
    return render(request, "food_app/order_history.html", {
        "orders": orders
    })


@login_required(login_url="login")
def track_order(request, order_id):
    """Real-time order tracking"""
    redirect_response = redirect_for_role(request)
    if redirect_response:
        return redirect_response

    order = get_object_or_404(Order, id=order_id, user=request.user)
    delivery = getattr(order, 'delivery', None)
    
    context = {
        "order": order,
        "delivery": delivery,
    }
    
    if delivery and delivery.delivery_person:
        context["delivery_person"] = delivery.delivery_person
        context["current_location"] = {
            "latitude": delivery.delivery_person.current_latitude,
            "longitude": delivery.delivery_person.current_longitude,
        }
    
    return render(request, "food_app/track_order.html", context)


@login_required(login_url="login")
def delivery_status_api(request, order_id):
    """API endpoint for delivery status (AJAX)"""
    redirect_response = redirect_for_role(request)
    if redirect_response:
        return redirect_response

    order = get_object_or_404(Order, id=order_id, user=request.user)
    delivery = getattr(order, 'delivery', None)
    
    if not delivery:
        return JsonResponse({"error": "No delivery found"}, status=404)
    
    data = {
        "status": delivery.status,
        "status_display": delivery.get_status_display(),
        "updated_at": delivery.updated_at.isoformat(),
        "estimated_delivery_time": delivery.estimated_delivery_time.isoformat() if delivery.estimated_delivery_time else None,
    }
    
    if delivery.delivery_person:
        data["delivery_person"] = {
            "name": delivery.delivery_person.user.get_full_name() or delivery.delivery_person.user.username,
            "phone": delivery.delivery_person.phone,
            "vehicle_info": delivery.delivery_person.vehicle_info,
            "current_latitude": delivery.delivery_person.current_latitude,
            "current_longitude": delivery.delivery_person.current_longitude,
        }
    
    return JsonResponse(data)
from django.contrib.auth.decorators import login_required

@login_required
def admin_dashboard(request):

    if get_user_role(request.user) != "admin":
        messages.error(request, "Access Denied!")
        return redirect("home")

    if request.method == "POST":
        inventory_action = request.POST.get("inventory_action")
        if inventory_action == "add":
            name = request.POST.get("name", "").strip()
            description = request.POST.get("description", "").strip()
            price = request.POST.get("price", "0")
            category_id = request.POST.get("category")
            available = request.POST.get("available") == "on"
            image = request.FILES.get("image")
            if name and category_id:
                category = get_object_or_404(Category, id=category_id)
                Product.objects.create(
                    category=category,
                    name=name,
                    description=description,
                    price=price,
                    available=available,
                    image=image,
                )
                messages.success(request, f"Product '{name}' added to inventory.")
            else:
                messages.error(request, "Please provide a product name and category.")
            return redirect("admin_dashboard")

        if inventory_action == "toggle":
            product_id = request.POST.get("product_id")
            if product_id:
                product = get_object_or_404(Product, id=product_id)
                product.available = not product.available
                product.save()
                messages.success(request, f"Availability updated for '{product.name}'.")
            return redirect("admin_dashboard")

        if inventory_action == "delete":
            product_id = request.POST.get("product_id")
            if product_id:
                product = get_object_or_404(Product, id=product_id)
                product_name = product.name
                product.delete()
                messages.success(request, f"Product '{product_name}' removed from inventory.")
            return redirect("admin_dashboard")

    orders = Order.objects.select_related("user", "delivery_address").prefetch_related("items__product", "delivery__delivery_person").all().order_by("-created_at")
    users = User.objects.filter(userprofile__isnull=False).select_related("userprofile").order_by("-date_joined")
    total_orders = orders.count()
    total_users = users.count()
    total_revenue = orders.aggregate(total_revenue=Sum("total"))["total_revenue"] or 0
    total_customers = UserProfile.objects.filter(role="customer").count()
    total_delivery_persons = UserProfile.objects.filter(role="delivery").count()
    total_admins = UserProfile.objects.filter(role="admin").count()
    delivery_persons = DeliveryPerson.objects.filter(user__userprofile__role="delivery")
    inventory_items = Product.objects.select_related("category").order_by("category__name", "name")
    categories = Category.objects.all()

    context = {
        "orders": orders,
        "users": users,
        "delivery_persons": delivery_persons,
        "inventory_items": inventory_items,
        "categories": categories,
        "total_orders": total_orders,
        "total_users": total_users,
        "total_revenue": total_revenue,
        "total_customers": total_customers,
        "total_delivery_persons": total_delivery_persons,
        "total_admins": total_admins,
    }

    return render(request, "food_app/admin_dashboard.html", context)


@require_POST
@login_required
def admin_create_user(request):
    """Admin-only: create a new user account with a chosen role."""
    if get_user_role(request.user) != "admin":
        messages.error(request, "Access Denied!")
        return redirect("home")

    username = request.POST.get("username", "").strip()
    email = request.POST.get("email", "").strip()
    password = request.POST.get("password", "")
    role = request.POST.get("role", "customer")
    phone = request.POST.get("phone", "").strip()

    if role not in dict(UserProfile.ROLE_CHOICES):
        role = "customer"

    if not username or not password:
        messages.error(request, "Username and password are required.")
        return redirect("admin_dashboard")

    if User.objects.filter(username=username).exists():
        messages.error(request, f"Username '{username}' already exists.")
        return redirect("admin_dashboard")

    user = User.objects.create_user(username=username, email=email, password=password)
    UserProfile.objects.filter(user=user).update(role=role)

    if role == "delivery":
        DeliveryPerson.objects.get_or_create(user=user, defaults={"phone": phone})

    messages.success(request, f"User '{username}' created as {role}.")
    return redirect("admin_dashboard")


@require_POST
@login_required
def update_user_role(request, user_id):
    """Admin-only: change a user's role (customer/admin/delivery)."""
    if get_user_role(request.user) != "admin":
        messages.error(request, "Access Denied!")
        return redirect("home")

    target_user = get_object_or_404(User, id=user_id)
    new_role = request.POST.get("role")

    if new_role not in dict(UserProfile.ROLE_CHOICES):
        messages.error(request, "Invalid role selected.")
        return redirect("admin_dashboard")

    if target_user == request.user and new_role != "admin":
        messages.error(request, "You cannot remove your own admin role.")
        return redirect("admin_dashboard")

    if target_user.userprofile.role == "admin" and new_role != "admin":
        remaining_admins = UserProfile.objects.filter(role="admin").exclude(user=target_user).count()
        if remaining_admins == 0:
            messages.error(request, "Cannot change role: at least one admin must remain.")
            return redirect("admin_dashboard")

    UserProfile.objects.filter(user=target_user).update(role=new_role)

    if new_role == "delivery":
        DeliveryPerson.objects.get_or_create(user=target_user, defaults={"phone": ""})

    messages.success(request, f"Updated {target_user.username}'s role to {new_role}.")
    return redirect("admin_dashboard")


@require_POST
@login_required
def admin_delete_user(request, user_id):
    """Admin-only: permanently remove a user account."""
    if get_user_role(request.user) != "admin":
        messages.error(request, "Access Denied!")
        return redirect("home")

    target_user = get_object_or_404(User, id=user_id)

    if target_user == request.user:
        messages.error(request, "You cannot remove your own account.")
        return redirect("admin_dashboard")

    if get_user_role(target_user) == "admin":
        remaining_admins = UserProfile.objects.filter(role="admin").exclude(user=target_user).count()
        if remaining_admins == 0:
            messages.error(request, "Cannot remove the only remaining admin.")
            return redirect("admin_dashboard")

    username = target_user.username
    target_user.delete()
    messages.success(request, f"User '{username}' has been removed.")
    return redirect("admin_dashboard")


@require_POST
@login_required
def assign_delivery_person(request, order_id):
    if get_user_role(request.user) != "admin":
        messages.error(request, "Access Denied!")
        return redirect("home")

    order = get_object_or_404(Order, id=order_id)
    delivery = getattr(order, "delivery", None)
    if not delivery:
        messages.error(request, "Order delivery record not found.")
        return redirect("admin_dashboard")

    delivery_person_id = request.POST.get("delivery_person")
    if not delivery_person_id:
        messages.error(request, "Please select a delivery person to assign.")
        return redirect("admin_dashboard")

    delivery_person = get_object_or_404(DeliveryPerson, id=delivery_person_id)
    delivery.delivery_person = delivery_person
    if not delivery.delivery_code:
        delivery.delivery_code = generate_delivery_code()
    if delivery.status == "pending":
        delivery.status = "assigned"
    delivery.save()
    sync_order_status(delivery)

    messages.success(request, f"Order #{order.id} assigned to {delivery_person.user.username}.")

    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse({"success": True, "message": f"Order #{order.id} assigned successfully."})

    return redirect("admin_dashboard")

@require_POST
@login_required
def accept_delivery(request, delivery_id):
    if request.user.userprofile.role != "delivery":
        messages.error(request, "Access Denied!")
        return redirect("home")

    delivery = get_object_or_404(Delivery, id=delivery_id, delivery_person__user=request.user)
    if delivery.status != "assigned":
        messages.error(request, "This delivery cannot be accepted right now.")
        return redirect("delivery_dashboard")

    delivery.status = "picked_up"
    delivery.save()
    sync_order_status(delivery)

    messages.success(request, f"You have accepted Order #{delivery.order.id}.")

    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse({"success": True, "message": f"You accepted Order #{delivery.order.id}."})

    return redirect("delivery_dashboard")

@require_POST
@login_required
def mark_delivery_delivered(request, delivery_id):
    if request.user.userprofile.role != "delivery":
        messages.error(request, "Access Denied!")
        return redirect("home")

    delivery = get_object_or_404(Delivery, id=delivery_id, delivery_person__user=request.user)
    if delivery.status not in ["picked_up", "in_transit", "assigned"]:
        error_message = "This delivery is already completed or cannot be marked delivered."
        messages.error(request, error_message)
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({"success": False, "message": error_message}, status=400)
        return redirect("delivery_dashboard")

    entered_code = (request.POST.get("delivery_code") or "").strip()
    if not entered_code:
        error_message = "Delivery verification code is required."
        messages.error(request, error_message)
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({"success": False, "message": error_message}, status=400)
        return redirect("delivery_dashboard")

    if entered_code != delivery.delivery_code:
        error_message = "Invalid delivery verification code."
        messages.error(request, error_message)
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({"success": False, "message": error_message}, status=400)
        return redirect("delivery_dashboard")

    delivery.status = "delivered"
    delivery.actual_delivery_time = timezone.now()
    delivery.save()
    sync_order_status(delivery)

    messages.success(request, f"Order #{delivery.order.id} marked as delivered.")

    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse({"success": True, "message": f"Order #{delivery.order.id} marked as delivered."})

    return redirect("delivery_dashboard")

@login_required(login_url="login")
def api_deliveries(request):
    """API endpoint returning deliveries assigned to the logged-in delivery person."""
    if get_user_role(request.user) != "delivery":
        return JsonResponse({"error": "Access denied"}, status=403)

    delivery_person = DeliveryPerson.objects.filter(user=request.user).first()
    deliveries = Delivery.objects.none()
    if delivery_person:
        deliveries = (
            Delivery.objects.select_related("order__user", "order__delivery_address")
            .filter(delivery_person=delivery_person)
            .order_by("-created_at")
        )

    payload = {
        "count": deliveries.count(),
        "deliveries": [
            {
                "id": delivery.id,
                "order_id": delivery.order.id,
                "status": delivery.status,
                "status_display": delivery.get_status_display(),
                "total": float(delivery.order.total),
                "customer": delivery.order.user.username,
                "address": delivery.order.delivery_address.street_address if delivery.order.delivery_address else None,
                "city": delivery.order.delivery_address.city if delivery.order.delivery_address else None,
                "phone": delivery.order.delivery_address.phone if delivery.order.delivery_address else None,
                # Note: delivery_code is intentionally excluded here. The customer
                # provides it to the delivery person; it must not be exposed via this API.
            }
            for delivery in deliveries
        ],
    }
    return JsonResponse(payload)


@login_required
def delivery_dashboard(request):
    if get_user_role(request.user) != "delivery":
        messages.error(request, "Access Denied!")
        return redirect("home")

    delivery_person = DeliveryPerson.objects.filter(user=request.user).first()
    assigned_deliveries = Delivery.objects.none()
    completed_deliveries = Delivery.objects.none()
    total_earnings = 0
    if delivery_person:
        assigned_deliveries = Delivery.objects.select_related("order__user", "order__delivery_address").filter(delivery_person=delivery_person).order_by("-created_at")
        completed_deliveries = assigned_deliveries.filter(status="delivered")
        total_earnings = completed_deliveries.aggregate(total_earnings=Sum("order__total"))["total_earnings"] or 0

    return render(request, "food_app/delivery_dashboard.html", {
        "assigned_deliveries": assigned_deliveries,
        "completed_deliveries": completed_deliveries,
        "completed_count": completed_deliveries.count(),
        "active_count": assigned_deliveries.filter(status__in=["assigned", "picked_up", "in_transit"]).count(),
        "total_earnings": total_earnings,
    })