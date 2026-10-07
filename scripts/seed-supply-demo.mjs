// Deterministic, isolated simulation dataset. No production tables are modified.
import 'dotenv/config';
import pg from 'pg';
import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';

pg.types.setTypeParser(1082, value => value); // PostgreSQL DATE is a calendar date, not an instant.

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const version = 'supply-demo-20261003-v1';
const baseline = '2026-10-05T00:00:00+08:00';
const known = '2026-10-05T07:00:00+08:00';
const end = '2026-10-19T00:00:00+08:00';
const time = (day, hour = 8) => new Date(Date.parse(baseline) + (day * 24 + hour) * 3600000).toISOString();
const quote = value => '"' + value.replaceAll('"', '""') + '"';
const definitions = [
  ['work_calendars', 'name text NOT NULL, timezone text NOT NULL'],
  ['calendar_days', 'calendar_id text NOT NULL, work_date date NOT NULL, shift_start timestamptz NOT NULL, shift_end timestamptz NOT NULL, capacity_minutes integer NOT NULL CHECK(capacity_minutes>0), UNIQUE(scenario_id,calendar_id,work_date), CHECK(shift_end>shift_start)', { calendar_id: 'work_calendars' }],
  ['factories', 'name text NOT NULL, timezone text NOT NULL, calendar_id text NOT NULL', { calendar_id: 'work_calendars' }],
  ['warehouses', 'factory_id text NOT NULL, name text NOT NULL', { factory_id: 'factories' }],
  ['people', 'name text NOT NULL, role text NOT NULL, factory_id text', { factory_id: 'factories' }],
  ['materials', 'name text NOT NULL, specification text NOT NULL, base_unit text NOT NULL, revision_controlled boolean NOT NULL'],
  ['material_revisions', 'material_id text NOT NULL, revision text NOT NULL, valid_from timestamptz NOT NULL, valid_to timestamptz NOT NULL, UNIQUE(scenario_id,material_id,revision), CHECK(valid_to>valid_from)', { material_id: 'materials' }],
  ['material_code_mappings', 'external_system text NOT NULL, external_code text NOT NULL, material_id text NOT NULL, factory_id text NOT NULL, valid_from timestamptz NOT NULL, valid_to timestamptz NOT NULL, UNIQUE(scenario_id,external_system,external_code,factory_id), CHECK(valid_to>valid_from)', { material_id: 'materials', factory_id: 'factories' }],
  ['unit_conversions', 'material_id text NOT NULL, factory_id text NOT NULL, from_unit text NOT NULL, to_unit text NOT NULL, factor numeric(18,6) NOT NULL CHECK(factor>0), valid_from timestamptz NOT NULL, valid_to timestamptz NOT NULL, confirmed_by text NOT NULL, evidence text NOT NULL, CHECK(valid_to>valid_from)', { material_id: 'materials', factory_id: 'factories', confirmed_by: 'people' }],
  ['products', 'name text NOT NULL, revision text NOT NULL'],
  ['production_lines', 'factory_id text NOT NULL, calendar_id text NOT NULL, name text NOT NULL, daily_capacity integer NOT NULL CHECK(daily_capacity>0)', { factory_id: 'factories', calendar_id: 'work_calendars' }],
  ['bom_headers', 'product_id text NOT NULL, factory_id text NOT NULL, version integer NOT NULL, valid_from timestamptz NOT NULL, valid_to timestamptz NOT NULL, approval_status text NOT NULL CHECK(approval_status IN (\'approved\',\'draft\')), UNIQUE(scenario_id,product_id,factory_id,version), CHECK(valid_to>valid_from)', { product_id: 'products', factory_id: 'factories' }],
  ['bom_lines', 'bom_id text NOT NULL, line_no integer NOT NULL, material_id text NOT NULL, revision_id text NOT NULL, quantity_per numeric(18,6) NOT NULL CHECK(quantity_per>0), unit text NOT NULL, UNIQUE(scenario_id,bom_id,line_no)', { bom_id: 'bom_headers', material_id: 'materials', revision_id: 'material_revisions' }],
  ['inventory_snapshots', 'snapshot_at timestamptz NOT NULL, factory_id text NOT NULL, warehouse_id text NOT NULL, material_id text NOT NULL, revision_id text NOT NULL, lot_no text NOT NULL, quantity numeric(18,6) NOT NULL CHECK(quantity>=0), unit text NOT NULL, quality_status text NOT NULL, UNIQUE(scenario_id,snapshot_at,warehouse_id,lot_no)', { factory_id: 'factories', warehouse_id: 'warehouses', material_id: 'materials', revision_id: 'material_revisions' }],
  ['production_plan_lines', 'plan_no text NOT NULL, line_no integer NOT NULL, version integer NOT NULL, factory_id text NOT NULL, line_id text NOT NULL, product_id text NOT NULL, quantity numeric(18,6) NOT NULL CHECK(quantity>=0), need_at timestamptz NOT NULL, status text NOT NULL CHECK(status IN (\'active\',\'superseded\')), production_order text NOT NULL, effective_at timestamptz NOT NULL, promised_at timestamptz NOT NULL, UNIQUE(scenario_id,plan_no,line_no,version)', { factory_id: 'factories', line_id: 'production_lines', product_id: 'products' }],
  ['inventory_reservations', 'snapshot_id text NOT NULL, plan_id text, external_order text, quantity numeric(18,6) NOT NULL CHECK(quantity>0), effective_at timestamptz NOT NULL, released_at timestamptz, status text NOT NULL, deduction_scope text NOT NULL CHECK(deduction_scope IN (\'included_demand\',\'external_demand\')), CHECK((plan_id IS NOT NULL) <> (external_order IS NOT NULL))', { snapshot_id: 'inventory_snapshots', plan_id: 'production_plan_lines' }],
  ['quality_holds', 'snapshot_id text NOT NULL, quantity numeric(18,6) NOT NULL CHECK(quantity>0), reason text NOT NULL, started_at timestamptz NOT NULL, released_at timestamptz, release_evidence text', { snapshot_id: 'inventory_snapshots' }],
  ['suppliers', 'name text NOT NULL, contact_person text NOT NULL, qualification text NOT NULL'],
  ['supplier_materials', 'supplier_id text NOT NULL, material_id text NOT NULL, lead_hours integer NOT NULL CHECK(lead_hours>0), UNIQUE(scenario_id,supplier_id,material_id)', { supplier_id: 'suppliers', material_id: 'materials' }],
  ['purchase_order_lines', 'order_no text NOT NULL, line_no integer NOT NULL, supplier_id text NOT NULL, material_id text NOT NULL, revision_id text NOT NULL, factory_id text NOT NULL, quantity numeric(18,6) NOT NULL CHECK(quantity>0), delivered_quantity numeric(18,6) NOT NULL CHECK(delivered_quantity>=0), cancelled_quantity numeric(18,6) NOT NULL CHECK(cancelled_quantity>=0), unit text NOT NULL, status text NOT NULL, UNIQUE(scenario_id,order_no,line_no), CHECK(delivered_quantity+cancelled_quantity<=quantity)', { supplier_id: 'suppliers', material_id: 'materials', revision_id: 'material_revisions', factory_id: 'factories' }],
  ['supplier_commitments', 'purchase_line_id text NOT NULL, installment text NOT NULL, version integer NOT NULL, quantity numeric(18,6) NOT NULL CHECK(quantity>0), original_available_at timestamptz NOT NULL, available_at timestamptz NOT NULL, changed_at timestamptz NOT NULL, known_at timestamptz NOT NULL, feedback text NOT NULL, evidence_ref text NOT NULL, UNIQUE(scenario_id,purchase_line_id,installment,version)', { purchase_line_id: 'purchase_order_lines' }],
  ['transfer_routes', 'from_factory text NOT NULL, to_factory text NOT NULL, transport_hours integer NOT NULL CHECK(transport_hours>0), handling_hours integer NOT NULL CHECK(handling_hours>=0), capacity numeric(18,6) NOT NULL CHECK(capacity>0), cutoff_at timestamptz NOT NULL, cost_per_unit numeric(18,2) NOT NULL CHECK(cost_per_unit>=0), currency text NOT NULL, restrictions text NOT NULL, CHECK(from_factory<>to_factory)', { from_factory: 'factories', to_factory: 'factories' }],
  ['expedite_options', 'purchase_line_id text NOT NULL, supplier_id text NOT NULL, available_quantity numeric(18,6) NOT NULL CHECK(available_quantity>0), ready_at timestamptz NOT NULL, available_at timestamptz NOT NULL, transport_mode text NOT NULL, total_cost numeric(18,2) NOT NULL CHECK(total_cost>=0), currency text NOT NULL, valid_until timestamptz NOT NULL, evidence_ref text NOT NULL', { purchase_line_id: 'purchase_order_lines', supplier_id: 'suppliers' }],
  ['supply_rules', 'factory_id text NOT NULL, material_id text, rule_type text NOT NULL, version integer NOT NULL, value jsonb NOT NULL, valid_from timestamptz NOT NULL, valid_to timestamptz NOT NULL, approved_by text NOT NULL, evidence text NOT NULL', { factory_id: 'factories', material_id: 'materials', approved_by: 'people' }],
  ['responsibility_rules', 'factory_id text NOT NULL, material_id text NOT NULL, owner_id text NOT NULL, escalation_owner_id text NOT NULL', { factory_id: 'factories', material_id: 'materials', owner_id: 'people', escalation_owner_id: 'people' }],
  ['approval_policies', 'action_type text NOT NULL, version integer NOT NULL, max_quantity numeric(18,6) NOT NULL, max_cost numeric(18,2) NOT NULL, approver_role text NOT NULL, requires_approval boolean NOT NULL, valid_from timestamptz NOT NULL, valid_to timestamptz NOT NULL'],
  ['approved_substitutions', 'material_id text NOT NULL, substitute_material_id text NOT NULL, revision_id text NOT NULL, substitute_revision_id text NOT NULL, product_id text NOT NULL, factory_id text NOT NULL, ratio numeric(18,6) NOT NULL CHECK(ratio>0), approval_ref text NOT NULL, approved_by text NOT NULL, valid_from timestamptz NOT NULL, valid_to timestamptz NOT NULL', { material_id: 'materials', substitute_material_id: 'materials', revision_id: 'material_revisions', substitute_revision_id: 'material_revisions', product_id: 'products', factory_id: 'factories', approved_by: 'people' }],
  ['scheduling_constraints', 'line_id text NOT NULL, from_product text NOT NULL, to_product text NOT NULL, changeover_minutes integer NOT NULL CHECK(changeover_minutes>=0), changeover_cost numeric(18,2) NOT NULL, earliest_start timestamptz NOT NULL, latest_finish timestamptz NOT NULL, requires_kitting boolean NOT NULL', { line_id: 'production_lines', from_product: 'products', to_product: 'products' }],
  ['risk_events', 'factory_id text NOT NULL, material_id text NOT NULL, shortage_at timestamptz NOT NULL, shortage_quantity numeric(18,6) NOT NULL CHECK(shortage_quantity>0), affected_plan_id text NOT NULL, owner_id text NOT NULL, status text NOT NULL, detected_at timestamptz NOT NULL, updated_at timestamptz NOT NULL', { factory_id: 'factories', material_id: 'materials', affected_plan_id: 'production_plan_lines', owner_id: 'people' }],
  ['investigation_evidence', 'risk_id text NOT NULL, fact_status text NOT NULL CHECK(fact_status IN (\'confirmed\',\'hypothesis\')), source_table text NOT NULL, source_id text NOT NULL, known_at timestamptz NOT NULL, statement text NOT NULL, tool_input jsonb NOT NULL, tool_output jsonb NOT NULL', { risk_id: 'risk_events' }],
  ['plan_versions', 'risk_id text NOT NULL, version integer NOT NULL, action_type text NOT NULL, quantity numeric(18,6) NOT NULL CHECK(quantity>0), available_at timestamptz NOT NULL, total_cost numeric(18,2) NOT NULL CHECK(total_cost>=0), currency text NOT NULL, assumptions jsonb NOT NULL, valid_until timestamptz NOT NULL, feasible boolean NOT NULL, status text NOT NULL, UNIQUE(scenario_id,risk_id,version)', { risk_id: 'risk_events' }],
  ['approvals', 'plan_id text NOT NULL, policy_id text NOT NULL, approver_id text NOT NULL, decision text NOT NULL, decided_at timestamptz NOT NULL, approved_quantity numeric(18,6) NOT NULL, approved_cost numeric(18,2) NOT NULL, opinion text NOT NULL', { plan_id: 'plan_versions', policy_id: 'approval_policies', approver_id: 'people' }],
  ['execution_orders', 'plan_id text NOT NULL, approval_id text NOT NULL, operation_key text NOT NULL, factory_id text NOT NULL, from_factory text, material_id text NOT NULL, revision_id text NOT NULL, quantity numeric(18,6) NOT NULL CHECK(quantity>0), unit text NOT NULL, business_document text NOT NULL, status text NOT NULL, created_at timestamptz NOT NULL, UNIQUE(scenario_id,operation_key)', { plan_id: 'plan_versions', approval_id: 'approvals', factory_id: 'factories', from_factory: 'factories', material_id: 'materials', revision_id: 'material_revisions' }],
  ['shipment_lines', 'purchase_line_id text, execution_id text, material_id text NOT NULL, quantity numeric(18,6) NOT NULL CHECK(quantity>0), actual_shipped_at timestamptz, eta_at timestamptz NOT NULL, status text NOT NULL, CHECK((purchase_line_id IS NOT NULL) <> (execution_id IS NOT NULL))', { purchase_line_id: 'purchase_order_lines', execution_id: 'execution_orders', material_id: 'materials' }],
  ['receipt_lines', 'shipment_id text NOT NULL, warehouse_id text NOT NULL, lot_no text NOT NULL, quantity numeric(18,6) NOT NULL CHECK(quantity>0), received_at timestamptz NOT NULL', { shipment_id: 'shipment_lines', warehouse_id: 'warehouses' }],
  ['quality_releases', 'receipt_id text NOT NULL, quantity numeric(18,6) NOT NULL CHECK(quantity>0), released_at timestamptz NOT NULL, status text NOT NULL, evidence text NOT NULL', { receipt_id: 'receipt_lines' }],
  ['line_deliveries', 'release_id text NOT NULL, line_id text NOT NULL, quantity numeric(18,6) NOT NULL CHECK(quantity>0), expected_at timestamptz NOT NULL, actual_at timestamptz, status text NOT NULL', { release_id: 'quality_releases', line_id: 'production_lines' }],
  ['execution_events', 'execution_id text NOT NULL, status text NOT NULL, event_at timestamptz NOT NULL, evidence_ref text NOT NULL', { execution_id: 'execution_orders' }],
  ['operation_attempts', 'operation_key text NOT NULL, attempt_no integer NOT NULL, execution_id text NOT NULL, response_status text NOT NULL, requested_at timestamptz NOT NULL, UNIQUE(scenario_id,operation_key,attempt_no)', { execution_id: 'execution_orders' }],
  ['closure_records', 'risk_id text NOT NULL, execution_id text NOT NULL, line_delivery_id text NOT NULL, checked_at timestamptz NOT NULL, observed_until timestamptz NOT NULL, remaining_shortage numeric(18,6) NOT NULL CHECK(remaining_shortage=0), evidence text NOT NULL', { risk_id: 'risk_events', execution_id: 'execution_orders', line_delivery_id: 'line_deliveries' }],
  ['import_test_files', 'group_id text NOT NULL, file_name text NOT NULL, ordinal integer NOT NULL, payload jsonb NOT NULL, expected_status text NOT NULL, UNIQUE(scenario_id,group_id,ordinal)'],
  ['expected_results', 'test_name text NOT NULL, as_of timestamptz NOT NULL, expected jsonb NOT NULL, rationale text NOT NULL, UNIQUE(scenario_id,test_name)'],
  ['dataset_status', 'table_name text NOT NULL, availability text NOT NULL CHECK(availability IN (\'provided\',\'confirmed_empty\',\'known_missing\')), explanation text NOT NULL, UNIQUE(scenario_id,table_name)'],
];

const scenarios = [
  ['normal', '正常供应', 'P001', [400,400,400], 800, 0],
  ['supplier_delay', '供应商延期', 'P001', [400,400,400], 800, 400],
  ['partial_transfer', '部分调拨', 'P001', [400,400,400], 800, 400],
  ['late_transfer', '调拨过晚', 'P001', [400,400,400], 800, 400],
  ['quality_hold', '质量冻结', 'P001', [600,0,0], 800, 100],
  ['missing_unit', '单位换算缺失', 'P002', [100,0,0], 20, null],
  ['bom_revision', 'BOM 版本变化', 'P003', [100,0,0], 300, 0],
  ['unreleased_receipt', '收货未放行', 'P001', [400,400,400], 800, 400],
  ['execution_pending', '执行未完成', 'P001', [400,400,400], 800, 400],
  ['atomic_failure', '整批导入失败', 'P001', [100,100,100], 800, 0],
  ['duplicate_submit', '重复提交', 'P001', [400,400,400], 800, 400],
  ['closed_loop', '补给到线后关闭', 'P001', [400,400,400], 800, 0],
];
const rows = Object.fromEntries(definitions.map(([name]) => [name, []]));
function add(table, scenario, id, values, occurred = known, ingested = occurred) {
  const row = { scenario_id: scenario, id, ...values, source_system: 'SIMULATED_SUPPLY_V1', source_record_id: `${scenario}:${table}:${id}`, occurred_at: occurred, ingested_at: ingested, is_simulated: true };
  rows[table].push(row); return row;
}
const materialNames = ['整车控制器','连接器','制动传感器','线束','保险丝','冷却管','动力支架','座椅电机','车灯模组','门锁总成','密封件','备用控制器'];
const mat = n => `P${String(n).padStart(3,'0')}`;
const revision = (m, v = 1) => `${m}-R${v}`;

for (const [s, name, focus, demand, opening, shortage] of scenarios) {
  const special = ['quality_hold','missing_unit','bom_revision'].includes(s);
  add('work_calendars',s,'CAL',{name:'模拟两周连续日班',timezone:'Asia/Shanghai'});
  for (let d=0;d<14;d++) add('calendar_days',s,`DAY-${d}`,{calendar_id:'CAL',work_date:`2026-10-${String(5+d).padStart(2,'0')}`,shift_start:time(d,8),shift_end:time(d,16),capacity_minutes:480});
  for (const f of ['A','B']) {
    add('factories',s,f,{name:`${f} 工厂（模拟）`,timezone:'Asia/Shanghai',calendar_id:'CAL'});
    add('warehouses',s,`WH-${f}`,{factory_id:f,name:`${f} 厂零件库`});
    for (const role of ['planner','quality','manager']) add('people',s,`${role}-${f}`,{name:`模拟${f}厂${role}`,role,factory_id:f});
    add('production_lines',s,`LINE-${f}`,{factory_id:f,calendar_id:'CAL',name:`${f}厂装配线`,daily_capacity:800});
  }
  for (let i=1;i<=12;i++) {
    const m=mat(i);
    add('materials',s,m,{name:materialNames[i-1],specification:`DEMO-SPEC-${i}`,base_unit:'件',revision_controlled:i===3});
    add('material_revisions',s,revision(m),{material_id:m,revision:'R1',valid_from:'2026-01-01T00:00:00+08:00',valid_to:i===3?'2026-10-05T00:00:00+08:00':'2027-01-01T00:00:00+08:00'});
    if(i===3) add('material_revisions',s,revision(m,2),{material_id:m,revision:'R2',valid_from:baseline,valid_to:'2027-01-01T00:00:00+08:00'});
    for(const f of ['A','B']) {
      add('material_code_mappings',s,`${m}-${f}`,{external_system:'ERP_DEMO',external_code:`ERP-${m}`,material_id:m,factory_id:f,valid_from:baseline,valid_to:end});
      if (!(s==='missing_unit'&&m==='P002'&&f==='A')) add('unit_conversions',s,`${m}-${f}`,{material_id:m,factory_id:f,from_unit:'箱',to_unit:'件',factor:24,valid_from:baseline,valid_to:end,confirmed_by:`quality-${f}`,evidence:`模拟包装规格 DEMO-PACK-${m}`});
      let quantity=100000,unit='件';
      if(m===focus&&f==='A') quantity=opening;
      if(m==='P001'&&f==='B') quantity=s==='partial_transfer'?1650:2400;
      if(m==='P002'&&f==='A') { if(s==='missing_unit'){quantity=20;unit='箱';}else{quantity=5000;unit='箱';} }
      add('inventory_snapshots',s,`INV-${f}-${m}`,{snapshot_at:baseline,factory_id:f,warehouse_id:`WH-${f}`,material_id:m,revision_id:revision(m,i===3?2:1),lot_no:`LOT-${f}-${m}`,quantity,unit,quality_status:s==='quality_hold'&&m==='P001'&&f==='A'?'partially_held':'released'},baseline);
      add('responsibility_rules',s,`${m}-${f}`,{factory_id:f,material_id:m,owner_id:`planner-${f}`,escalation_owner_id:`manager-${f}`});
    }
  }
  for (let i=1;i<=4;i++) add('suppliers',s,`SUP-${i}`,{name:`模拟供应商${i}`,contact_person:`模拟供应联络人${i}`,qualification:'approved_demo'});
  for(let i=1;i<=12;i++) add('supplier_materials',s,mat(i),{supplier_id:`SUP-${(i-1)%4+1}`,material_id:mat(i),lead_hours:48});
  for(const p of ['CAR-1','CAR-2']) {
    add('products',s,p,{name:p==='CAR-1'?'模拟轿车':'模拟SUV',revision:'2026'});
    for(const f of ['A','B']) for(const v of [1,2]) {
      const b=`BOM-${p}-${f}-${v}`;
      add('bom_headers',s,b,{product_id:p,factory_id:f,version:v,valid_from:v===1?'2026-01-01T00:00:00+08:00':baseline,valid_to:v===1?baseline:'2027-01-01T00:00:00+08:00',approval_status:'approved'});
      const parts=p==='CAR-1'?[1,2,3,4,5,6]:[7,8,9,10,11];
      for (const i of parts) add('bom_lines',s,`${b}-${i}`,{bom_id:b,line_no:i,material_id:mat(i),revision_id:revision(mat(i),i===3?v:1),quantity_per:i===3&&v===2?2:1,unit:'件'});
    }
  }
  for(const f of ['A','B']) for(let d=0;d<14;d++) for(const p of ['CAR-1','CAR-2']) {
    const quantity=p==='CAR-2'?50:f==='B'?100:d<3?demand[d]:special?0:100;
    add('production_plan_lines',s,`PLAN-${f}-${p}-${d}`,{plan_no:`PP-${f}`,line_no:d*2+(p==='CAR-1'?1:2),version:2,factory_id:f,line_id:`LINE-${f}`,product_id:p,quantity,need_at:time(d,16),status:'active',production_order:`MO-${f}-${p}-${d}`,effective_at:known,promised_at:time(d+1,16)});
  }
  add('production_plan_lines',s,'OLD-PLAN',{plan_no:'PP-A',line_no:1,version:1,factory_id:'A',line_id:'LINE-A',product_id:'CAR-1',quantity:999,need_at:time(0,16),status:'superseded',production_order:'MO-A-CAR-1-0',effective_at:time(-1,7),promised_at:time(1,16)},time(-1,7));
  add('inventory_reservations',s,'RES-INCLUDED',{snapshot_id:'INV-A-P004',plan_id:'PLAN-A-CAR-1-0',external_order:null,quantity:50,effective_at:known,released_at:null,status:'active',deduction_scope:'included_demand'});
  add('inventory_reservations',s,'RES-EXTERNAL',{snapshot_id:'INV-A-P005',plan_id:null,external_order:'SERVICE-ORDER-001',quantity:30,effective_at:known,released_at:null,status:'active',deduction_scope:'external_demand'});
  if(s==='quality_hold') add('quality_holds',s,'HOLD-1',{snapshot_id:'INV-A-P001',quantity:300,reason:'模拟抽检异常，300件冻结；与占用无重叠',started_at:baseline,released_at:null,release_evidence:null},baseline);
  for (const f of ['A','B']) {
    const rules={safety_stock:{quantity:100,unit:'件'},reservation:{included_demand:'not_deducted_twice',external_demand:'deduct',holds_overlap:false},receiving:{hours:1},inspection:{hours:1},line_delivery:{hours:1},calendar:{calendar_id:'CAL'},shortage_granularity:{unit:'day',need_hour:16}};
    for(const [type,value] of Object.entries(rules)) add('supply_rules',s,`${f}-${type}`,{factory_id:f,material_id:null,rule_type:type,version:1,value,valid_from:baseline,valid_to:end,approved_by:`manager-${f}`,evidence:'模拟业务口径确认单 DEMO-RULE-001'});
    add('scheduling_constraints',s,`SCHED-${f}`,{line_id:`LINE-${f}`,from_product:'CAR-1',to_product:'CAR-2',changeover_minutes:30,changeover_cost:500,earliest_start:time(0,8),latest_finish:time(13,16),requires_kitting:true});
    add('approved_substitutions',s,`SUB-${f}`,{material_id:'P001',substitute_material_id:'P012',revision_id:'P001-R1',substitute_revision_id:'P012-R1',product_id:'CAR-1',factory_id:f,ratio:1,approval_ref:`DEMO-QUAL-SUB-${f}`,approved_by:`quality-${f}`,valid_from:baseline,valid_to:end});
  }
  for(const action of ['transfer','expedite','reschedule','substitute']) add('approval_policies',s,action,{action_type:action,version:1,max_quantity:1000,max_cost:10000,approver_role:'manager',requires_approval:true,valid_from:baseline,valid_to:end});
  add('transfer_routes',s,'B-A',{from_factory:'B',to_factory:'A',transport_hours:s==='late_transfer'?72:4,handling_hours:2,capacity:1000,cutoff_at:time(1,8),cost_per_unit:2,currency:'CNY',restrictions:'同版本且质量放行；调出后覆盖B厂完整预测窗口需求'});
  add('transfer_routes',s,'A-B',{from_factory:'A',to_factory:'B',transport_hours:4,handling_hours:2,capacity:1000,cutoff_at:time(1,8),cost_per_unit:2,currency:'CNY',restrictions:'需审批；不得挪用已占用数量'});

  function purchase(id,m,q,available,delivered=0) {
    const supplier=`SUP-${(Number(m.slice(1))-1)%4+1}`;
    add('purchase_order_lines',s,id,{order_no:`PO-${id}`,line_no:1,supplier_id:supplier,material_id:m,revision_id:revision(m,m==='P003'?2:1),factory_id:'A',quantity:q,delivered_quantity:delivered,cancelled_quantity:0,unit:'件',status:delivered===q?'received':'open'});
    // Original and revised commitments are histories, never additive supplies.
    add('supplier_commitments',s,`${id}-V1`,{purchase_line_id:id,installment:'01',version:1,quantity:q,original_available_at:time(1,8),available_at:time(1,8),changed_at:time(-1,10),known_at:time(-1,10),feedback:'模拟原始承诺',evidence_ref:`DEMO-REPLY-${id}-1`},time(-1,10));
    add('supplier_commitments',s,`${id}-V2`,{purchase_line_id:id,installment:'01',version:2,quantity:q,original_available_at:time(1,8),available_at:available,changed_at:time(0,6),known_at:known,feedback:available>time(1,8)?'已确认交期推迟；设备故障原因仍待核实':'已确认按期供货',evidence_ref:`DEMO-REPLY-${id}-2`},time(0,6),known);
  }
  if(!special) {
    const available=time(['normal','atomic_failure'].includes(s)?1:3,8);
    purchase('PO-FOCUS','P001',400,available,s==='unreleased_receipt'?400:0);
    purchase('PO-FOLLOWUP','P001',10000,time(3,8));
    add('expedite_options',s,'EXP-1',{purchase_line_id:'PO-FOCUS',supplier_id:'SUP-1',available_quantity:s==='partial_transfer'?150:400,ready_at:time(1,8),available_at:time(2,12),transport_mode:'模拟加急公路',total_cost:3000,currency:'CNY',valid_until:time(1,18),evidence_ref:'DEMO-QUOTE-001'});
  }
  // A separate material has completed logistics, to exercise all join stages without double counting.
  purchase('PO-LOGISTICS','P004',100,time(0,10),100);
  add('shipment_lines',s,'SHIP-BASE',{purchase_line_id:'PO-LOGISTICS',execution_id:null,material_id:'P004',quantity:100,actual_shipped_at:time(-1,8),eta_at:time(0,7),status:'received'},time(-1,8));
  add('receipt_lines',s,'RECEIPT-BASE',{shipment_id:'SHIP-BASE',warehouse_id:'WH-A',lot_no:'LOT-RECEIVED-P004',quantity:100,received_at:time(0,7)},time(0,7));
  add('quality_releases',s,'RELEASE-BASE',{receipt_id:'RECEIPT-BASE',quantity:100,released_at:time(0,9),status:'released',evidence:'DEMO-QA-BASE'},time(0,9));
  add('line_deliveries',s,'LINE-BASE',{release_id:'RELEASE-BASE',line_id:'LINE-A',quantity:100,expected_at:time(0,10),actual_at:time(0,10),status:'delivered'},time(0,10));
  if(s==='unreleased_receipt') {
    add('shipment_lines',s,'SHIP-HELD',{purchase_line_id:'PO-FOCUS',execution_id:null,material_id:'P001',quantity:400,actual_shipped_at:time(0,8),eta_at:time(1,6),status:'received'},time(0,8));
    add('receipt_lines',s,'RECEIPT-HELD',{shipment_id:'SHIP-HELD',warehouse_id:'WH-A',lot_no:'LOT-HELD',quantity:400,received_at:time(1,6)},time(1,6));
    add('quality_releases',s,'RELEASE-HELD',{receipt_id:'RECEIPT-HELD',quantity:400,released_at:time(3,7),status:'released',evidence:'DEMO-QA-LATE'},time(3,7));
    add('line_deliveries',s,'LINE-HELD',{release_id:'RELEASE-HELD',line_id:'LINE-A',quantity:400,expected_at:time(3,8),actual_at:time(3,8),status:'delivered'},time(3,8));
  }
  const riskScenario=shortage>0||s==='closed_loop';
  if(riskScenario) {
    const riskQty=s==='closed_loop'?400:shortage;
    const day=s==='quality_hold'?0:2;
    add('risk_events',s,'RISK-1',{factory_id:'A',material_id:focus,shortage_at:time(day,16),shortage_quantity:riskQty,affected_plan_id:`PLAN-A-CAR-1-${day}`,owner_id:'planner-A',status:s==='closed_loop'?'closed':['execution_pending','duplicate_submit'].includes(s)?'executing':'investigating',detected_at:known,updated_at:s==='closed_loop'?time(3,16):known},known);
    add('investigation_evidence',s,'EVIDENCE-1',{risk_id:'RISK-1',fact_status:'confirmed',source_table:s==='quality_hold'?'quality_holds':'supplier_commitments',source_id:s==='quality_hold'?'HOLD-1':'PO-FOCUS-V2',known_at:known,statement:s==='quality_hold'?'300件质量冻结已确认':'补货交期推迟已确认',tool_input:{factory:'A',material:focus,as_of:known},tool_output:{evidence_only:true}},known);
    add('investigation_evidence',s,'EVIDENCE-2',{risk_id:'RISK-1',fact_status:'hypothesis',source_table:'risk_events',source_id:'RISK-1',known_at:known,statement:'设备故障原因待供应商确认，不作为已确认事实',tool_input:{request:'supplier_cause'},tool_output:{confirmed:false}},known);
    add('plan_versions',s,'PLAN-TRANSFER',{risk_id:'RISK-1',version:1,action_type:'transfer',quantity:s==='partial_transfer'?250:riskQty,available_at:time(s==='late_transfer'?4:1,14),total_cost:(s==='partial_transfer'?250:riskQty)*2,currency:'CNY',assumptions:{route:'B-A',donor_horizon:'14 days',quality_release_required:true},valid_until:time(1,8),feasible:!['late_transfer','quality_hold'].includes(s),status:s==='closed_loop'?'executed':['execution_pending','duplicate_submit'].includes(s)?'approved':'candidate'});
    if(!special) add('plan_versions',s,'PLAN-EXPEDITE',{risk_id:'RISK-1',version:2,action_type:'expedite',quantity:s==='partial_transfer'?150:400,available_at:time(2,12),total_cost:3000,currency:'CNY',assumptions:{quote:'EXP-1',replaces_purchase_supply:'PO-FOCUS',not_additive:true},valid_until:time(1,18),feasible:true,status:'candidate'});
    if(['execution_pending','duplicate_submit','closed_loop'].includes(s)) {
      add('approvals',s,'APP-1',{plan_id:'PLAN-TRANSFER',policy_id:'transfer',approver_id:'manager-A',decision:'approved',decided_at:time(1,7),approved_quantity:400,approved_cost:800,opinion:'模拟审批：确认调出厂无缺料，批准该方案版本'},time(1,7));
      add('execution_orders',s,'EXEC-1',{plan_id:'PLAN-TRANSFER',approval_id:'APP-1',operation_key:'OP-TRANSFER-001',factory_id:'A',from_factory:'B',material_id:'P001',revision_id:'P001-R1',quantity:400,unit:'件',business_document:'DEMO-TRANSFER-001',status:s==='closed_loop'?'completed':'created',created_at:time(1,8)},time(1,8));
      add('execution_events',s,'EVENT-CREATED',{execution_id:'EXEC-1',status:'created',event_at:time(1,8),evidence_ref:'execution_orders:EXEC-1'},time(1,8));
      add('operation_attempts',s,'TRY-1',{operation_key:'OP-TRANSFER-001',attempt_no:1,execution_id:'EXEC-1',response_status:s==='duplicate_submit'?'response_timeout_after_commit':'success',requested_at:time(1,8)},time(1,8));
      if(s==='duplicate_submit') add('operation_attempts',s,'TRY-2',{operation_key:'OP-TRANSFER-001',attempt_no:2,execution_id:'EXEC-1',response_status:'returned_existing_document',requested_at:time(1,9)},time(1,9));
      if(s==='closed_loop') {
        add('shipment_lines',s,'SHIP-TRANSFER',{purchase_line_id:null,execution_id:'EXEC-1',material_id:'P001',quantity:400,actual_shipped_at:time(1,8),eta_at:time(1,12),status:'received'},time(1,8));
        add('receipt_lines',s,'RECEIPT-TRANSFER',{shipment_id:'SHIP-TRANSFER',warehouse_id:'WH-A',lot_no:'LOT-TRANSFER',quantity:400,received_at:time(1,12)},time(1,12));
        add('quality_releases',s,'RELEASE-TRANSFER',{receipt_id:'RECEIPT-TRANSFER',quantity:400,released_at:time(1,13),status:'released',evidence:'DEMO-QA-TRANSFER'},time(1,13));
        add('line_deliveries',s,'LINE-TRANSFER',{release_id:'RELEASE-TRANSFER',line_id:'LINE-A',quantity:400,expected_at:time(1,14),actual_at:time(1,14),status:'delivered'},time(1,14));
        for(const [i,status,hour] of [[1,'shipped',8],[2,'received',12],[3,'released',13],[4,'line_available',14]]) add('execution_events',s,`EVENT-${i}`,{execution_id:'EXEC-1',status,event_at:time(1,hour),evidence_ref:status==='shipped'?'shipment_lines:SHIP-TRANSFER':status==='received'?'receipt_lines:RECEIPT-TRANSFER':status==='released'?'quality_releases:RELEASE-TRANSFER':'line_deliveries:LINE-TRANSFER'},time(1,hour));
        add('closure_records',s,'CLOSE-1',{risk_id:'RISK-1',execution_id:'EXEC-1',line_delivery_id:'LINE-TRANSFER',checked_at:time(2,16),observed_until:time(3,16),remaining_shortage:0,evidence:'模拟到线400件；复算周三缺口为0，观察至周四未复发'},time(3,16));
      }
    }
  }
  if(s==='atomic_failure') {
    add('import_test_files',s,'FILE-1',{group_id:'GROUP-FAIL',file_name:'01_update.csv',ordinal:1,payload:{material:'P001',old_quantity:800,new_quantity:999},expected_status:'rolled_back'});
    add('import_test_files',s,'FILE-2',{group_id:'GROUP-FAIL',file_name:'02_invalid.csv',ordinal:2,payload:{material:'P002',quantity:-1},expected_status:'rejected_negative_quantity'});
  }
  add('expected_results',s,'SHORTAGE',{test_name:'focus_shortage',as_of:s==='closed_loop'?time(3,16):known,expected:{material:focus,factory:'A',first_three_days_max_shortage:shortage,first_three_days_demand:s==='bom_revision'?200:demand.reduce((a,b)=>a+b,0),computable:s!=='missing_unit',first_shortage_at:shortage?time(s==='quality_hold'?0:2,16):null},rationale:'固定场景人工预设答案；库存+可用到货-有效BOM需求，前3日窗口'});
  if(s==='partial_transfer') add('expected_results',s,'TRANSFER',{test_name:'donor_surplus',as_of:known,expected:{donor_opening:1650,donor_14_day_demand:1400,transfer_limit:250,residual_shortage:150},rationale:'不得挪用B厂两周生产需求'});
  if(s==='late_transfer') add('expected_results',s,'LATE',{test_name:'transfer_arrival',as_of:known,expected:{timely:false},rationale:'周二08时发运+72小时运输+2小时处理，晚于周三16时需求'});
  if(s==='execution_pending') add('expected_results',s,'OPEN',{test_name:'closure',as_of:time(2,16),expected:{closed:false},rationale:'仅有建单记录，没有收货放行到线证据'});
  if(s==='atomic_failure') add('expected_results',s,'ATOMIC',{test_name:'rollback',as_of:known,expected:{original_quantity:800,remaining_written_rows:0},rationale:'第二条写入违反非负约束，回滚前面的更新'});
  if(s==='duplicate_submit') add('expected_results',s,'IDEMPOTENT',{test_name:'idempotency',as_of:time(1,9),expected:{attempts:2,execution_orders:1},rationale:'同一业务操作标识只对应一个执行单据'});
  if(s==='closed_loop') add('expected_results',s,'CLOSED',{test_name:'closure',as_of:time(3,16),expected:{closed:true,remaining_shortage:0},rationale:'实际到线数量已覆盖需求，观察窗口结束'});
}

// Final-state rows become known only when that state actually exists. Earlier states
// are represented by their event/history rows, not by backdating the final status.
const temporalCorrections=[];
for(const row of rows.risk_events) if(row.status==='closed') temporalCorrections.push(['risk_events',row,time(3,16)]);
for(const row of rows.execution_orders) if(row.status==='completed') temporalCorrections.push(['execution_orders',row,time(1,14)]);
for(const row of rows.shipment_lines) if(row.status==='received') {
  const receipt=rows.receipt_lines.find(r=>r.scenario_id===row.scenario_id&&r.shipment_id===row.id);
  temporalCorrections.push(['shipment_lines',row,receipt.received_at]);
}
for(const row of rows.purchase_order_lines) if(row.delivered_quantity>0) {
  const shipment=rows.shipment_lines.find(r=>r.scenario_id===row.scenario_id&&r.purchase_line_id===row.id);
  const receipt=rows.receipt_lines.find(r=>r.scenario_id===row.scenario_id&&r.shipment_id===shipment.id);
  temporalCorrections.push(['purchase_order_lines',row,receipt.received_at]);
}
for(const [,row,t] of temporalCorrections){row.occurred_at=t;row.ingested_at=t;}

for(const [s] of scenarios) for(const [table] of definitions.filter(([t])=>t!=='dataset_status')) {
  const count=rows[table].filter(r=>r.scenario_id===s).length;
  add('dataset_status',s,table,{table_name:table,availability:count?'provided':'confirmed_empty',explanation:count?`提供 ${count} 条模拟记录`:'该场景确认无此类业务记录'});
}
add('dataset_status','missing_unit','missing-conversion',{table_name:'unit_conversions:P002:A',availability:'known_missing',explanation:'故意缺失P002在A厂的箱到件规则，不能猜测换算系数'});
const digest=createHash('sha256').update(JSON.stringify({version,scenarios,rows})).digest('hex');

const client=new pg.Client({connectionString:process.env.ANALYST_DATABASE_URL,connectionTimeoutMillis:5000,statement_timeout:60000});
let inTransaction=false;
try {
  await client.connect();
  await client.query('BEGIN'); inTransaction=true;
  await client.query("SELECT pg_advisory_xact_lock(hashtext('supply_demo_seed_v1'))");
  const exists=(await client.query("SELECT to_regnamespace('supply_demo') AS schema")).rows[0].schema;
  if(exists) {
    const marker=(await client.query("SELECT to_regclass('supply_demo.dataset_manifest') AS marker")).rows[0].marker;
    assert(marker,'supply_demo 已存在但不是本生成器管理的数据，停止以避免覆盖。');
    const saved=(await client.query('SELECT version,sha256 FROM supply_demo.dataset_manifest')).rows;
    assert.equal(saved.length,1); assert.equal(saved[0].version,version); assert.equal(saved[0].sha256,digest,'已有版本不同；不覆盖已有数据。');
  } else {
    await client.query('CREATE SCHEMA supply_demo');
    await client.query("COMMENT ON SCHEMA supply_demo IS '汽车供应链模拟数据；按scenario_id隔离，不是真实运营数据'");
    await client.query('CREATE TABLE supply_demo.dataset_manifest(version text PRIMARY KEY, sha256 text NOT NULL, generated_at timestamptz NOT NULL DEFAULT now(), seed integer NOT NULL, specification text NOT NULL)');
    await client.query('CREATE TABLE supply_demo.scenarios(id text PRIMARY KEY, name text NOT NULL, window_start timestamptz NOT NULL, window_end timestamptz NOT NULL, initial_as_of timestamptz NOT NULL, focus_material text NOT NULL, is_simulated boolean NOT NULL CHECK(is_simulated), notes text NOT NULL)');
    for(const [s,name,focus] of scenarios) await client.query('INSERT INTO supply_demo.scenarios VALUES($1,$2,$3,$4,$5,$6,true,$7)',[s,name,baseline,end,known,focus,'固定日级模拟；记录含未来事件，回放必须按ingested_at过滤；规则有效期左闭右开。']);
    for(const [table,ddl,refs={}] of definitions) {
      const foreign=Object.entries(refs).map(([column,target])=>`, FOREIGN KEY(scenario_id,${quote(column)}) REFERENCES supply_demo.${quote(target)}(scenario_id,id)`).join('');
      await client.query(`CREATE TABLE supply_demo.${quote(table)}(scenario_id text NOT NULL REFERENCES supply_demo.scenarios(id), id text NOT NULL, ${ddl}, source_system text NOT NULL, source_record_id text NOT NULL, occurred_at timestamptz NOT NULL, ingested_at timestamptz NOT NULL, is_simulated boolean NOT NULL CHECK(is_simulated), PRIMARY KEY(scenario_id,id), UNIQUE(source_record_id), CHECK(ingested_at>=occurred_at) ${foreign})`);
      await client.query(`COMMENT ON TABLE supply_demo.${quote(table)} IS '模拟数据；禁止不带scenario_id混合计算；历史回放需按ingested_at过滤'`);
      const data=rows[table],columns=Object.keys(data[0]);
      // Bounded scenario fixtures; parameterized batches avoid SQL escaping and round trips.
      for(let start=0;start<data.length;start+=100) {
        const chunk=data.slice(start,start+100),values=[];
        const tuples=chunk.map(row=>'('+columns.map(c=>{const value=row[c];values.push(value!==null&&typeof value==='object'?JSON.stringify(value):value);return '$'+values.length;}).join(',')+')');
        await client.query(`INSERT INTO supply_demo.${quote(table)}(${columns.map(quote).join(',')}) VALUES ${tuples.join(',')}`,values);
      }
    }
    await client.query('INSERT INTO supply_demo.dataset_manifest(version,sha256,seed,specification) VALUES($1,$2,20261003,$3)',[version,digest,'汽车供应链Agent_数据准备方案.md；完全确定性生成，不使用随机数量']);
  }

  const checks=[];
  async function zero(name,sql) { const result=(await client.query(sql)).rows; assert.equal(result.length,0,`${name}: ${JSON.stringify(result.slice(0,3))}`);checks.push(name); }
  await zero('BOM有效期不重叠',`SELECT a.id FROM supply_demo.bom_headers a JOIN supply_demo.bom_headers b ON a.scenario_id=b.scenario_id AND a.product_id=b.product_id AND a.factory_id=b.factory_id AND a.id<b.id AND tstzrange(a.valid_from,a.valid_to,'[)') && tstzrange(b.valid_from,b.valid_to,'[)')`);
  await zero('计划按当时有效BOM及物料版本展开',`SELECT p.id FROM supply_demo.production_plan_lines p LEFT JOIN supply_demo.bom_headers b ON b.scenario_id=p.scenario_id AND b.factory_id=p.factory_id AND b.product_id=p.product_id AND p.need_at>=b.valid_from AND p.need_at<b.valid_to WHERE p.status='active' GROUP BY p.scenario_id,p.id HAVING count(b.id)<>1`);
  await zero('BOM行物料与版本一致',`SELECT l.id FROM supply_demo.bom_lines l JOIN supply_demo.material_revisions r ON (r.scenario_id,r.id)=(l.scenario_id,l.revision_id) WHERE l.material_id<>r.material_id`);
  await zero('产线每日产能足够',`SELECT p.scenario_id,p.line_id FROM supply_demo.production_plan_lines p JOIN supply_demo.production_lines l ON (l.scenario_id,l.id)=(p.scenario_id,p.line_id) WHERE p.status='active' GROUP BY p.scenario_id,p.line_id,p.need_at,l.daily_capacity HAVING sum(p.quantity)>l.daily_capacity`);
  await zero('收货不超过发运数量',`SELECT s.id FROM supply_demo.shipment_lines s JOIN supply_demo.receipt_lines r ON (r.scenario_id,r.shipment_id)=(s.scenario_id,s.id) GROUP BY s.scenario_id,s.id,s.quantity HAVING sum(r.quantity)>s.quantity`);
  await zero('放行不超过收货且时间正确',`SELECT q.id FROM supply_demo.quality_releases q JOIN supply_demo.receipt_lines r ON (r.scenario_id,r.id)=(q.scenario_id,q.receipt_id) WHERE q.quantity>r.quantity OR q.released_at<r.received_at`);
  await zero('到线不超过放行且时间正确',`SELECT l.id FROM supply_demo.line_deliveries l JOIN supply_demo.quality_releases q ON (q.scenario_id,q.id)=(l.scenario_id,l.release_id) WHERE l.quantity>q.quantity OR l.actual_at<q.released_at`);
  await zero('收货与采购已交数量核销',`SELECT p.id FROM supply_demo.purchase_order_lines p LEFT JOIN supply_demo.shipment_lines s ON (s.scenario_id,s.purchase_line_id)=(p.scenario_id,p.id) LEFT JOIN supply_demo.receipt_lines r ON (r.scenario_id,r.shipment_id)=(s.scenario_id,s.id) GROUP BY p.scenario_id,p.id,p.delivered_quantity HAVING coalesce(sum(r.quantity),0)<>p.delivered_quantity`);
  await zero('审批对应准确方案数量和费用',`SELECT a.id FROM supply_demo.approvals a JOIN supply_demo.plan_versions p ON (p.scenario_id,p.id)=(a.scenario_id,a.plan_id) WHERE a.approved_quantity<>p.quantity OR a.approved_cost<>p.total_cost`);
  await zero('最终状态不提前获知',`SELECT id FROM supply_demo.risk_events WHERE status='closed' AND ingested_at<updated_at UNION ALL SELECT s.id FROM supply_demo.shipment_lines s JOIN supply_demo.receipt_lines r ON (r.scenario_id,r.shipment_id)=(s.scenario_id,s.id) WHERE s.status='received' AND s.ingested_at<r.received_at`);

  // Independent SQL calculation; expected values above are explicit test oracles, not computed from these queries.
  const results=[];
  for(const [s,,focus] of scenarios) {
    const expected=rows.expected_results.find(r=>r.scenario_id===s&&r.id==='SHORTAGE').expected;
    const asOf=s==='closed_loop'?time(3,16):known;
    const openingResult=(await client.query(`SELECT sum(CASE WHEN i.unit=m.base_unit THEN i.quantity WHEN u.factor IS NOT NULL THEN i.quantity*u.factor END) AS qty,
      bool_and(i.unit=m.base_unit OR u.factor IS NOT NULL) AS computable
      FROM supply_demo.inventory_snapshots i JOIN supply_demo.materials m ON (m.scenario_id,m.id)=(i.scenario_id,i.material_id)
      LEFT JOIN supply_demo.unit_conversions u ON u.scenario_id=i.scenario_id AND u.material_id=i.material_id AND u.factory_id=i.factory_id AND u.from_unit=i.unit AND i.snapshot_at>=u.valid_from AND i.snapshot_at<u.valid_to
      WHERE i.scenario_id=$1 AND i.factory_id='A' AND i.material_id=$2`,[s,focus])).rows[0];
    const demandRows=(await client.query(`SELECT p.need_at,sum(p.quantity*l.quantity_per)::text AS qty FROM supply_demo.production_plan_lines p
      JOIN supply_demo.bom_headers b ON b.scenario_id=p.scenario_id AND b.factory_id=p.factory_id AND b.product_id=p.product_id AND p.need_at>=b.valid_from AND p.need_at<b.valid_to
      JOIN supply_demo.bom_lines l ON (l.scenario_id,l.bom_id)=(b.scenario_id,b.id)
      WHERE p.scenario_id=$1 AND p.factory_id='A' AND p.status='active' AND l.material_id=$2 AND p.need_at<$3 AND p.ingested_at<=$4 GROUP BY p.need_at ORDER BY p.need_at`,[s,focus,time(3,0),asOf])).rows;
    assert.equal(demandRows.reduce((n,r)=>n+Number(r.qty),0),expected.first_three_days_demand);
    assert.equal(openingResult.computable,expected.computable);
    if(!openingResult.computable){results.push({scenario:s,computable:false,reason:'缺少单位换算'});continue;}
    const held=Number((await client.query(`SELECT coalesce(sum(h.quantity),0) AS qty FROM supply_demo.quality_holds h JOIN supply_demo.inventory_snapshots i ON (i.scenario_id,i.id)=(h.scenario_id,h.snapshot_id) WHERE h.scenario_id=$1 AND i.material_id=$2 AND i.factory_id='A' AND h.started_at<=$3 AND (h.released_at IS NULL OR h.released_at>$3)`,[s,focus,asOf])).rows[0].qty);
    const commitments=(await client.query(`SELECT DISTINCT ON(c.purchase_line_id,c.installment) c.available_at AS at,c.quantity::text AS qty FROM supply_demo.supplier_commitments c JOIN supply_demo.purchase_order_lines p ON (p.scenario_id,p.id)=(c.scenario_id,c.purchase_line_id) WHERE c.scenario_id=$1 AND p.material_id=$2 AND p.factory_id='A' AND p.delivered_quantity=0 AND c.known_at<=$3 ORDER BY c.purchase_line_id,c.installment,c.version DESC`,[s,focus,asOf])).rows;
    const actual=(await client.query(`SELECT l.actual_at AS at,l.quantity::text AS qty FROM supply_demo.line_deliveries l JOIN supply_demo.quality_releases q ON (q.scenario_id,q.id)=(l.scenario_id,l.release_id) JOIN supply_demo.receipt_lines r ON (r.scenario_id,r.id)=(q.scenario_id,q.receipt_id) JOIN supply_demo.shipment_lines sh ON (sh.scenario_id,sh.id)=(r.scenario_id,r.shipment_id) WHERE l.scenario_id=$1 AND sh.material_id=$2 AND l.ingested_at<=$3`,[s,focus,asOf])).rows;
    let cumulative=0,maxShortage=0,firstShortage=null;const balances=[];
    for(const d of demandRows){cumulative+=Number(d.qty);const received=[...commitments,...actual].filter(r=>new Date(r.at)<=new Date(d.need_at)).reduce((n,r)=>n+Number(r.qty),0);const balance=Number(openingResult.qty)-held+received-cumulative;balances.push(balance);maxShortage=Math.max(maxShortage,-balance);if(balance<0&&!firstShortage)firstShortage=new Date(d.need_at).toISOString();}
    assert.equal(maxShortage,expected.first_three_days_max_shortage,`${s}: shortage`);assert.equal(firstShortage,expected.first_shortage_at,`${s}: time`);
    results.push({scenario:s,computable:true,balances,maxShortage});
  }
  checks.push('12个场景的前三日需求、缺口、首次缺料时间及不可计算状态');
  const donor=(await client.query(`SELECT sum(p.quantity*l.quantity_per) AS qty FROM supply_demo.production_plan_lines p JOIN supply_demo.bom_headers b ON b.scenario_id=p.scenario_id AND b.factory_id=p.factory_id AND b.product_id=p.product_id AND p.need_at>=b.valid_from AND p.need_at<b.valid_to JOIN supply_demo.bom_lines l ON (l.scenario_id,l.bom_id)=(b.scenario_id,b.id) WHERE p.scenario_id='partial_transfer' AND p.factory_id='B' AND p.status='active' AND l.material_id='P001'`)).rows[0];
  assert.equal(Number(donor.qty),1400);assert.equal(1650-Number(donor.qty),250);checks.push('B厂两周余量250件，剩余缺口150件');
  const late=(await client.query("SELECT cutoff_at+(transport_hours+handling_hours)*interval '1 hour' AS arrives FROM supply_demo.transfer_routes WHERE scenario_id='late_transfer' AND id='B-A'")).rows[0];assert(new Date(late.arrives)>new Date(time(2,16)));checks.push('调拨过晚不可覆盖需求');
  assert.equal((await client.query("SELECT count(*)::int n FROM supply_demo.closure_records WHERE scenario_id='execution_pending'")).rows[0].n,0);
  await zero('关闭依赖实际到线和完整观察窗口',`SELECT c.id FROM supply_demo.closure_records c JOIN supply_demo.line_deliveries l ON (l.scenario_id,l.id)=(c.scenario_id,c.line_delivery_id) WHERE l.actual_at IS NULL OR l.actual_at>c.checked_at OR c.observed_until<c.checked_at+interval '24 hours'`);
  assert.equal((await client.query("SELECT count(*)::int n FROM supply_demo.execution_orders WHERE scenario_id='duplicate_submit'")).rows[0].n,1);
  assert.equal((await client.query("SELECT count(*)::int n FROM supply_demo.operation_attempts WHERE scenario_id='duplicate_submit'")).rows[0].n,2);
  checks.push('重复请求2次，仅1个执行单据；未执行完不关闭');
  // Exercise real PostgreSQL rollback/uniqueness without retaining the test writes.
  await client.query('SAVEPOINT atomic_test');
  let rejected=false;
  try {
    await client.query("UPDATE supply_demo.inventory_snapshots SET quantity=999 WHERE scenario_id='atomic_failure' AND id='INV-A-P001'");
    await client.query("UPDATE supply_demo.inventory_snapshots SET quantity=-1 WHERE scenario_id='atomic_failure' AND id='INV-A-P002'");
  }catch(e){assert.equal(e.code,'23514');rejected=true;await client.query('ROLLBACK TO SAVEPOINT atomic_test');}
  assert(rejected);await client.query('RELEASE SAVEPOINT atomic_test');
  assert.equal(Number((await client.query("SELECT quantity FROM supply_demo.inventory_snapshots WHERE scenario_id='atomic_failure' AND id='INV-A-P001'")).rows[0].quantity),800);
  await client.query('SAVEPOINT duplicate_test');
  try {await client.query("INSERT INTO supply_demo.execution_orders SELECT scenario_id,'DUPLICATE-TEST',plan_id,approval_id,operation_key,factory_id,from_factory,material_id,revision_id,quantity,unit,business_document,status,created_at,source_system,'DUPLICATE-TEST',occurred_at,ingested_at,is_simulated FROM supply_demo.execution_orders WHERE scenario_id='duplicate_submit'");assert.fail('重复业务操作应被拒绝');}
  catch(e){assert.equal(e.code,'23505');await client.query('ROLLBACK TO SAVEPOINT duplicate_test');}
  await client.query('RELEASE SAVEPOINT duplicate_test');checks.push('真实PostgreSQL保存点回滚及操作唯一约束');
  const counts={};
  for(const [table] of definitions){
    const actual=(await client.query(`SELECT * FROM supply_demo.${quote(table)} ORDER BY scenario_id,id`)).rows;
    assert.equal(actual.length,rows[table].length,table);counts[table]=actual.length;
    const index=new Map(actual.map(r=>[r.scenario_id+':'+r.id,r]));
    for(const expectedRow of rows[table]) {
      const actualRow=index.get(expectedRow.scenario_id+':'+expectedRow.id);assert(actualRow);
      for(const [column,value] of Object.entries(expectedRow)) {
        let observed=actualRow[column];
        if(observed instanceof Date) observed=observed.toISOString();
        if(typeof value==='number') assert.equal(Number(observed),value,`${table}.${column}`);
        else if(typeof value==='string' && /T\d\d:\d\d/.test(value)) assert.equal(Date.parse(observed),Date.parse(value),`${table}.${column}`);
        else assert.deepEqual(observed,value,`${table}.${column}`);
      }
    }
  }
  checks.push('全部生成字段逐值核对，重复运行不增写');
  await client.query('COMMIT');inTransaction=false;
  const report={version,sha256:digest,seed:20261003,generatedAt:new Date().toISOString(),database:(await client.query('SELECT current_database() AS name')).rows[0].name,schema:'supply_demo',scenarioCount:scenarios.length,tableCount:definitions.length+2,totalRows:Object.values(counts).reduce((a,b)=>a+b,0)+scenarios.length+1,counts,checks,results};
  const output=path.join(root,'data','supply-demo');mkdirSync(output,{recursive:true});
  writeFileSync(path.join(output,'validation.json'),JSON.stringify(report,null,2)+'\n');
  writeFileSync(path.join(output,'expected-results.json'),JSON.stringify(rows.expected_results,null,2)+'\n');
  writeFileSync(path.join(output,'数据生成与校验报告.md'),`# 汽车供应链模拟数据生成与校验报告\n\n- 数据库：${report.database}\n- 数据区域：supply_demo\n- 版本：${version}\n- 固定种子：20261003（全量确定性生成）\n- 场景：${scenarios.length}\n- 表：${report.tableCount}\n- 总记录：${report.totalRows}\n- 时间窗口：2026-10-05 至 2026-10-18，Asia/Shanghai\n- 数据内容哈希：${digest}\n\n## 表与记录数\n\n| 表 | 记录数 |\n|---|---:|\n| scenarios | ${scenarios.length} |\n| dataset_manifest | 1 |\n${Object.entries(counts).map(([t,n])=>`| ${t} | ${n} |`).join('\n')}\n\n## 场景\n\n${scenarios.map(([id,name])=>`- ${id}：${name}`).join('\n')}\n\n## 已通过校验\n\n${checks.map(c=>'- '+c).join('\n')}\n\n## 使用说明\n\n全部为模拟数据；查询必须指定 scenario_id，避免不同场景相加。未来事件也保存在场景中，历史回放需按 ingested_at 或 known_at 过滤。供应承诺选择当时最新版本，与实际收货核销，不能重复计入。数据仓库中的运行记录是演示夹具，不表示真实 Agent 已执行。\n\n运行 node scripts/seed-supply-demo.mjs 可校验相同版本；不会覆盖已有不同版本的数据。当前页面尚未接入此 PostgreSQL Schema，本次不修改页面和导入接口。\n\nSQL 示例：\n\n\`\`\`sql\nSELECT * FROM supply_demo.scenarios;\nSELECT * FROM supply_demo.inventory_snapshots WHERE scenario_id = 'supplier_delay';\nSELECT * FROM supply_demo.expected_results WHERE scenario_id = 'partial_transfer';\n\`\`\`\n`);
  console.log(JSON.stringify(report,null,2));
} catch(e) {if(inTransaction)await client.query('ROLLBACK');console.error(`${e.code||e.name}: ${e.message}`);process.exitCode=1;}
finally {await client.end();}
