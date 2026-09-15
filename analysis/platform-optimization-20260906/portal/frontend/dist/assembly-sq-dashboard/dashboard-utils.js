(function initAssemblyDashboardUtils(root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.AssemblyDashboardUtils = api;
}(typeof globalThis !== 'undefined' ? globalThis : this, function createUtils() {
  function escapeHtml(value) {
    return String(value ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  function repairKnownMojibake(value) {
    return typeof value === 'string'
      ? value.replaceAll('閽冲伐(EX21)', '钳工(EX21)').replaceAll('闁藉啿浼#(EX21)', '钳工(EX21)')
      : value;
  }

  function normalizeRows(rows) {
    if (!Array.isArray(rows)) return [];
    return rows.map(row => Object.fromEntries(
      Object.entries(row).map(([key, value]) => [key, repairKnownMojibake(value)])
    ));
  }

  function inspectionTaskRate(selfQuantity, qcQuantity, reportedQuantity) {
    const denominator = Number(reportedQuantity) * 2;
    if (!Number.isFinite(denominator) || denominator <= 0) return 0;
    const numerator = Number(selfQuantity) + Number(qcQuantity);
    if (!Number.isFinite(numerator)) return 0;
    return Math.min(100, Math.round(numerator / denominator * 100));
  }

  return { escapeHtml, repairKnownMojibake, normalizeRows, inspectionTaskRate };
}));
