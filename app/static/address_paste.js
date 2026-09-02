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
  // newlines in some order, e.g. "小徐 138... 湖北..." or one per line.
  const withoutPhone = text.replace(phone, "\n");
  const segments = withoutPhone
    .split(/[\n,，]+/)
    .map((segment) => segment.trim())
    .filter(Boolean);

  let name = "";
  let address = "";
  if (segments.length >= 2) {
    name = segments[0];
    address = segments.slice(1).join(" ");
  } else if (segments.length === 1) {
    const spaceParts = segments[0].split(/\s+/).filter(Boolean);
    if (spaceParts.length >= 2) {
      name = spaceParts[0];
      address = spaceParts.slice(1).join(" ");
    } else {
      address = segments[0];
    }
  }

  // A real name is short; if what we split off is long, it's almost
  // certainly still part of the address (e.g. a one-line address with no
  // name at all) -- fold it back in rather than mis-labeling it as a name.
  if (name.length > 6) {
    address = [name, address].filter(Boolean).join("");
    name = "";
  }

  return { name, phone, address };
}

if (typeof module !== "undefined" && module.exports) module.exports = { parseAddressPasteText };
