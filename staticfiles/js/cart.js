
// cart.js - handles + / - button clicks and sends AJAX (Fetch) requests to update cart quantities.
// Expects each cart row to have data-item-id and an input.quantity element.
// Also expects a CSRF token input present in the page.
(function(){
  function getCookie(name) {
    // Basic cookie retrieval for CSRF token
    const value = `; ${document.cookie}`;
    const parts = value.split(`; ${name}=`);
    if (parts.length === 2) return parts.pop().split(';').shift();
  }
  const csrftoken = getCookie('csrftoken');

  function showToast(message, success=true){
    const toastEl = document.getElementById('cart-toast');
    if(!toastEl) return;
    toastEl.querySelector('.toast-body').textContent = message;
    if(success) toastEl.classList.remove('bg-danger'); else toastEl.classList.add('bg-danger');
    const bsToast = new bootstrap.Toast(toastEl, {delay: 2000});
    bsToast.show();
  }

  async function updateQuantity(itemId, quantity, row){
    try{
      const resp = await fetch(`/cart/update/${itemId}/`, {
        method: 'POST',
        headers: {
          'X-CSRFToken': csrftoken,
          'Accept': 'application/json',
        },
        body: new URLSearchParams({quantity})
      });
      if(!resp.ok){
        const err = await resp.json().catch(()=>({error:'Unknown'}));
        showToast('Failed to update cart: ' + (err.error||resp.status), false);
        return;
      }
      const data = await resp.json();
      // Update UI: item total and cart total
      if(row){
        const itemTotalEl = row.querySelector('.item-total');
        if(itemTotalEl) itemTotalEl.textContent = '$' + (data.item_total||0).toFixed(2);
        if(data.item_id && data.item_total === 0){
          // removed
          row.remove();
        }
      }
      const cartTotalEl = document.getElementById('cart-total');
      if(cartTotalEl) cartTotalEl.textContent = (data.cart_total||0).toFixed(2);

      showToast('Cart updated successfully', true);
    }catch(e){
      console.error(e);
      showToast('Error updating cart', false);
    }
  }

  document.addEventListener('DOMContentLoaded', function(){
    document.querySelectorAll('.cart-row').forEach(function(row){
      const itemId = row.dataset.itemId;
      const input = row.querySelector('.quantity');
      const btnInc = row.querySelector('.increase');
      const btnDec = row.querySelector('.decrease');
      if(btnInc){
        btnInc.addEventListener('click', function(e){
          let q = parseInt(input.value) || 0;
          q = q + 1;
          input.value = q;
          updateQuantity(itemId, q, row);
        });
      }
      if(btnDec){
        btnDec.addEventListener('click', function(e){
          let q = parseInt(input.value) || 0;
          q = q - 1;
          if(q < 0) q = 0;
          input.value = q;
          updateQuantity(itemId, q, row);
        });
      }
    });
  });
})();
