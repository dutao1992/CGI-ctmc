(function attachTraceScanUtils(globalScope) {
  "use strict";

  function normalizedSequence(value) {
    const raw = String(value ?? "").trim();
    if (!/^\d{1,4}$/.test(raw)) return null;
    return raw.padStart(4, "0");
  }

  function parseScanPayload(value) {
    const raw = String(value ?? "").trim();
    if (!raw) return { raw: "" };

    try {
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === "object") {
        const sequenceNo = normalizedSequence(parsed.sequence_no ?? parsed.serial_no ?? parsed.sequence);
        return {
          raw,
          materialCode: String(parsed.material_code ?? parsed.part_no ?? parsed.material ?? "").trim(),
          sequenceNo,
          assemblyOrder: String(parsed.component_order_no ?? parsed.assembly_order ?? parsed.order_no ?? "").trim(),
          inboundLabel: String(parsed.inbound_label ?? parsed.label ?? "").trim(),
        };
      }
    } catch {}

    try {
      const url = new URL(raw);
      const sequenceNo = normalizedSequence(url.searchParams.get("sequence_no") ?? url.searchParams.get("serial_no"));
      const materialCode = String(url.searchParams.get("material_code") ?? "").trim();
      if (sequenceNo || materialCode) return { raw, materialCode, sequenceNo };
    } catch {}

    const composite = raw.match(/^(.+?)\s*(?:\||\+|,|，|\t|\n)\s*(\d{1,4})$/);
    if (composite) {
      return { raw, materialCode: composite[1].trim(), sequenceNo: normalizedSequence(composite[2]) };
    }
    const sequenceNo = normalizedSequence(raw);
    return sequenceNo ? { raw, sequenceNo } : { raw };
  }

  const api = { normalizedSequence, parseScanPayload };
  globalScope.TraceScanUtils = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
