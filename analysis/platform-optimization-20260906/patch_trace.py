from pathlib import Path
root=Path(__file__).parent/'trace';p=root/'app.py';s=p.read_text();s=s.replace('def bootstrap(self, user: dict | None = None, selected_site: str | None = None)', 'def bootstrap(self, user: dict | None = None, selected_site: str | None = None, compact: bool = False)').replace('return self.bootstrap_v3(user, selected_site)','return self.bootstrap_v3(user, selected_site, compact=compact)').replace('def bootstrap_v3(self, user: dict, selected_site: str | None = None)', 'def bootstrap_v3(self, user: dict, selected_site: str | None = None, compact: bool = False)')
a=s.index('    def bootstrap_v3');b=s.index('    def analytics_dashboard',a);block=s[a:b]
block=block.replace('        parts = self.rows(', '        parts = [] if compact else self.rows(')
block=block.replace('        all_bound_parts = self.rows(\n            """','        all_bound_parts = [] if compact else self.rows(\n            f"""')
block=block.replace('            ORDER BY r.component_order_no,r.process_no,s.part_order_no,s.purchase_line,s.sequence_no\n            """\n        )','            WHERE r.site_code IN ({scope_marks}) AND b.site_code=r.site_code\n            ORDER BY r.component_order_no,r.process_no,s.part_order_no,s.purchase_line,s.sequence_no\n            """, scope_sites\n        )')
block=block.replace('        orders: list[dict] = []','''        binding_counts = {(r['site_code'], r['component_order_no']): r['n'] for r in self.rows(
            f"""SELECT r.site_code,r.component_order_no,COUNT(br.id) AS n
            FROM binding_records br JOIN component_trace_requirements r ON r.id=br.requirement_id
            JOIN operation_batches b ON b.id=br.batch_id JOIN srm_parts s ON s.id=br.srm_part_id
            WHERE r.site_code IN ({scope_marks}) AND b.site_code=r.site_code
            GROUP BY r.site_code,r.component_order_no""", scope_sites)} if compact else {}
        orders: list[dict] = []''')
block=block.replace('bound_count = len(visible_bindings)', 'bound_count = binding_counts.get((order["site_code"], order["order_no"]), 0) if compact else len(visible_bindings)')
block=block.replace('            "component_orders": orders,\n            "order_statuses": orders,','            "compact": compact,\n            **({} if compact else {"component_orders": orders, "order_statuses": orders}),')
block=block.replace('"analytics": self.analytics_dashboard(scope_sites, orders)','"analytics": None if compact else self.analytics_dashboard(scope_sites, orders)')
s=s[:a]+block+s[b:];a=s.index('        requirements = self.rows(',s.index('    def analytics_dashboard'));b=s.index('        component_totals:',a);block=s[a:b].replace('            """','            f"""',1).replace('WHERE r.active=1\n            """','WHERE r.active=1 AND r.site_code IN ({scope_marks})\n            """, scope_sites');s=s[:a]+block+s[b:]
s=s.replace('self.store.bootstrap(user, self.selected_site(user, allow_all=True))','self.store.bootstrap(user, self.selected_site(user, allow_all=True), compact=parse_qs(parsed.query).get("compact", [""])[0] == "1")')
p.write_text(s)
p=root/'static/app.js';s=p.read_text();s=s.replace('async function loadBootstrap() {\n  const data = await api("/api/bootstrap");','''async function loadBootstrap() {
  const data = await api("/api/bootstrap?compact=1");
  data.component_orders = data.component_orders || data.orders;
  data.order_statuses = data.order_statuses || data.orders;''')
s=s.replace('  renderAnalyticsBoard();\n  hydrateOrders(data.component_orders);\n  if (data.user.role === "ADMIN") await loadUsers();\n  if (data.user.role === "ADMIN" && data.user.site_code === "HQ") {\n    loadStorageStatus({ silent: true });\n  }','''  hydrateOrders(data.component_orders);
  if (["board", "master"].includes(location.hash.slice(1))) await ensureWorkspaceDetails();''')
s=s.replace('    if (view === "board") renderAnalyticsBoard();','    if (["board", "master"].includes(view)) ensureWorkspaceDetails().then(() => { if(view === "board")renderAnalyticsBoard(); }).catch((error) => toast(error.message));')
# Guard async completion against bootstrap/site replacement.
idx=s.index('async function loadBootstrap()')
s=s[:idx]+'''let workspaceDetailsRequest = null;
async function ensureWorkspaceDetails() {
  const base = state.bootstrap;
  if (!base || !base.compact) return;
  if (workspaceDetailsRequest?.base === base) return workspaceDetailsRequest.promise;
  const promise = api("/api/bootstrap").then((data) => {
    if (state.bootstrap !== base) throw new Error("站点已切换，请重新打开明细");
    state.bootstrap = data;
    renderCatalog(data);
    hydrateOrders(data.component_orders);
    return data;
  }).finally(() => { if (workspaceDetailsRequest?.base === base)workspaceDetailsRequest=null; });
  workspaceDetailsRequest = {base, promise};
  return promise;
}

'''+s[idx:]
s=s.replace('function openStatusDetail(status) {','async function openStatusDetail(status) {\n  try { await ensureWorkspaceDetails(); } catch(error) { toast(error.message); return; }')
p.write_text(s)
