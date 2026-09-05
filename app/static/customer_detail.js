(() => {
  const customerId = Number(window.location.pathname.split("/").filter(Boolean).pop());

  async function postJson(url, body) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    let payload = null;
    try {
      payload = await response.json();
    } catch (err) {
      payload = null;
    }
    return { ok: response.ok, status: response.status, payload };
  }

  // ---- Add address ----
  const addToggle = document.getElementById("addAddressToggle");
  const addForm = document.getElementById("addAddressForm");
  const addCancel = document.getElementById("addAddressCancel");
  const addSave = document.getElementById("addAddressSave");
  const addError = document.getElementById("addAddressError");

  addToggle?.addEventListener("click", () => {
    addForm.hidden = !addForm.hidden;
  });
  // Deep-link from the customer list page's "+ 新增地址" quick entry.
  if (addForm && new URLSearchParams(window.location.search).get("open_address_form") === "1") {
    addForm.hidden = false;
    addForm.scrollIntoView({ behavior: "smooth", block: "start" });
  }
  addCancel?.addEventListener("click", () => {
    addForm.hidden = true;
  });

  document.getElementById("addAddressPasteParse")?.addEventListener("click", () => {
    const pasteError = document.getElementById("addAddressPasteError");
    pasteError.hidden = true;
    const result = parseAddressPasteText(document.getElementById("addAddressPasteText").value);
    if (!result) {
      pasteError.textContent = "未能识别手机号，请手动填写（未修改任何字段）";
      pasteError.hidden = false;
      return;
    }
    if (result.name) document.getElementById("addAddressRecipientName").value = result.name;
    document.getElementById("addAddressPhone").value = result.phone;
    if (result.address) document.getElementById("addAddressAddress").value = result.address;
  });

  async function submitNewAddress(allowDuplicate) {
    addError.hidden = true;
    const recipientName = document.getElementById("addAddressRecipientName").value.trim();
    const address = document.getElementById("addAddressAddress").value.trim();
    if (!recipientName || !address) {
      addError.textContent = "收件人姓名和收货地址不能为空";
      addError.hidden = false;
      return;
    }
    const body = {
      recipient_name: recipientName,
      phone: document.getElementById("addAddressPhone").value.trim() || null,
      address,
      label: document.getElementById("addAddressLabel").value.trim() || null,
      is_default: document.getElementById("addAddressIsDefault").checked,
      allow_duplicate: allowDuplicate,
    };
    addSave.disabled = true;
    const { ok, status, payload } = await postJson(`/api/customers/${customerId}/addresses`, body);
    addSave.disabled = false;
    if (ok) {
      window.location.reload();
      return;
    }
    if (status === 409) {
      if (window.confirm("该客户已存在相同收货地址，是否仍然保存为新地址？")) {
        await submitNewAddress(true);
      }
      return;
    }
    addError.textContent = (payload && (payload.detail || payload.message)) || "保存失败";
    addError.hidden = false;
  }

  addSave?.addEventListener("click", () => submitNewAddress(false));

  // ---- Edit / delete / set-default (event delegation over each address card) ----
  document.querySelectorAll(".address-card").forEach((card) => {
    const addressId = card.dataset.addressId;
    const viewBlock = card.querySelector(".address-card-view");
    const editForm = card.querySelector(".address-edit-form");
    const editError = card.querySelector(".address-edit-error");

    card.querySelector(".address-edit-toggle")?.addEventListener("click", () => {
      viewBlock.hidden = true;
      editForm.hidden = false;
    });
    card.querySelector(".address-edit-cancel")?.addEventListener("click", () => {
      editForm.hidden = true;
      viewBlock.hidden = false;
    });
    card.querySelector(".address-edit-save")?.addEventListener("click", async () => {
      editError.hidden = true;
      const recipientName = card.querySelector(".address-edit-recipient-name").value.trim();
      const address = card.querySelector(".address-edit-address").value.trim();
      if (!recipientName || !address) {
        editError.textContent = "收件人姓名和收货地址不能为空";
        editError.hidden = false;
        return;
      }
      const body = {
        recipient_name: recipientName,
        phone: card.querySelector(".address-edit-phone").value.trim() || null,
        address,
        label: card.querySelector(".address-edit-label").value.trim() || null,
        is_default: card.querySelector(".address-edit-is-default").checked,
      };
      const { ok, payload } = await postJson(`/api/customer-addresses/${addressId}`, body);
      if (ok) {
        window.location.reload();
        return;
      }
      editError.textContent = (payload && (payload.detail || payload.message)) || "保存失败";
      editError.hidden = false;
    });
    card.querySelector(".address-set-default")?.addEventListener("click", async () => {
      const body = {
        recipient_name: card.querySelector(".address-edit-recipient-name").value,
        phone: card.querySelector(".address-edit-phone").value,
        address: card.querySelector(".address-edit-address").value,
        label: card.querySelector(".address-edit-label").value,
        is_default: true,
      };
      const { ok } = await postJson(`/api/customer-addresses/${addressId}`, body);
      if (ok) window.location.reload();
    });
    card.querySelector(".address-delete")?.addEventListener("click", async () => {
      if (!window.confirm("确认删除该收货地址？已用于历史订单的收货信息不受影响。")) return;
      const response = await fetch(`/api/customer-addresses/${addressId}/delete`, { method: "POST" });
      if (response.ok) window.location.reload();
    });
  });
})();
