// Rule-based (no AI, no network) parser for pasted shipping-info text, e.g.
// copied from a chat message: "小徐 13900001111 湖北武汉市洪山区xxx". Shared
// by the new-order page and the customer address management page.
//
// Returns { name, phone, address } when a China mobile number (11 digits,
// starting 13-19) is found, otherwise null -- never guesses a phone number,
// and never fabricates fields it can't identify.
function parseAddressPasteText(raw) {
  const text = (raw || "").trim();
  if (!text) return null;

  const phoneMatch = text.match(/(?<!\d)1[3-9]\d{9}(?!\d)/);
  if (!phoneMatch) return null;
  const phone = phoneMatch[0];

  // Labeled form: "收件人：小徐 / 电话：xxx / 地址：xxx" (each on its own line).
  const labelName = text.match(/(?:收件人|姓名|联系人)[：:]\s*([^\s,，\n]+)/);
  const labelAddress = text.match(/(?:收货地址|地址)[：:]\s*(.+)/);
  if (labelName || labelAddress) {
    return {
      name: labelName ? labelName[1].trim() : "",
      phone,
      address: labelAddress ? labelAddress[1].trim() : "",
    };
  }

  // Unlabeled form: name/phone/address separated by spaces, commas, or
  // newlines in some order, e.g. "小徐 138... 湖北..." or one per line, or
  // a name glued directly onto the phone within the same segment via a
  // colon or bare space, e.g. "张测试：138..."/"张测试:138..."/"张测试 138...".
  const segments = text
    .split(/[\n,，]+/)
    .map((segment) => segment.trim())
    .filter(Boolean);
  const phoneSegmentIndex = segments.findIndex((segment) => segment.includes(phone));

  let name = "";
  const addressParts = [];
  segments.forEach((segment, index) => {
    if (index !== phoneSegmentIndex) {
      addressParts.push(segment);
      return;
    }
    // Whatever sits directly before the phone within its own segment --
    // stripped of a trailing "："/":"/space -- is the name candidate. A real
    // name is short; something longer here is address text that merely
    // happens to share a line with the phone (e.g. "138... 湖北..."), so it
    // must stay part of the address, never get consumed as a name.
    const phoneIndex = segment.indexOf(phone);
    const before = segment.slice(0, phoneIndex).replace(/[：:\s]+$/, "").trim();
    const after = segment.slice(phoneIndex + phone.length).replace(/^[：:\s]+/, "").trim();
    if (before && before.length <= 6 && !/\d/.test(before)) {
      name = before;
      if (after) addressParts.push(after);
    } else {
      const leftover = [before, after].filter(Boolean).join(" ");
      if (leftover) addressParts.push(leftover);
    }
  });

  // No name was glued to the phone itself (e.g. "张测试，138..." or
  // "小徐\n138...\n湖北..." -- name is its own separate segment) -- fall
  // back to the leading remaining segment when it looks name-shaped.
  if (!name && addressParts.length && addressParts[0].length <= 6 && !/\d/.test(addressParts[0])) {
    name = addressParts.shift();
  }

  return { name, phone, address: addressParts.join(" ") };
}

if (typeof module !== "undefined" && module.exports) module.exports = { parseAddressPasteText };
