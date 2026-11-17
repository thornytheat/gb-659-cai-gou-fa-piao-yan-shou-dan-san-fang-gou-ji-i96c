const POST = document.body.dataset.endpoint;
const feedback = document.querySelector("#feedback");
const out = document.querySelector("#result");
// app.js attaches its own sample-runner to #run; replace the node so only the
// batch form submission below is bound.
const originalRun = document.querySelector("#run");
const submit = originalRun?.cloneNode(true);
if (submit) submit.textContent = "核销并落账";
originalRun?.replaceWith(submit);
const acceptanceTable = document.querySelector("#acceptance-table tbody");
const invoiceTable = document.querySelector("#invoice-table tbody");

const ACCEPTANCE_FIELDS = ["doc_no", "sku", "currency", "original_amount", "exchange_rate", "received_on"];
const INVOICE_FIELDS = ["doc_no", "sku", "currency", "original_amount", "exchange_rate", "invoiced_on"];

function rowValues(row, fields) {
  const values = {};
  for (const field of fields) {
    const input = row.querySelector(`[name="${field}"]`);
    if (input) values[field] = input.value.trim();
  }
  return values;
}

function editableRow(values, fields, marker) {
  const row = document.createElement("tr");
  row.dataset[marker] = "1";
  for (const field of fields) {
    const cell = document.createElement("td");
    const input = document.createElement("input");
    input.name = field;
    input.value = values[field] || "";
    input.setAttribute("aria-label", field);
    if (["original_amount", "exchange_rate"].includes(field)) input.className = "num";
    if (field === "currency") { input.maxLength = 3; input.size = 4; }
    cell.appendChild(input);
    row.appendChild(cell);
  }
  const action = document.createElement("td");
  const remove = document.createElement("button");
  remove.type = "button"; remove.className = "icon"; remove.textContent = "×";
  remove.addEventListener("click", () => row.remove());
  action.appendChild(remove);
  row.appendChild(action);
  return row;
}

document.querySelector("#add-acceptance")?.addEventListener("click", () => {
  acceptanceTable.appendChild(editableRow(
    {currency: "USD", exchange_rate: "", received_on: new Date().toISOString().slice(0, 10)},
    ACCEPTANCE_FIELDS, "draftAcceptance"
  ));
});

document.querySelector("#add-invoice")?.addEventListener("click", () => {
  invoiceTable.appendChild(editableRow(
    {currency: "USD", exchange_rate: "", invoiced_on: new Date().toISOString().slice(0, 10)},
    INVOICE_FIELDS, "draftInvoice"
  ));
});

function collect() {
  const acceptances = [];
  for (const row of acceptanceTable.querySelectorAll("tr")) {
    // Posted rows are read-only but still travel with the batch: their
    // expected_balance / version let the server reject a batch planned against
    // a balance someone else already consumed.
    const values = rowValues(row, ACCEPTANCE_FIELDS);
    const acceptanceId = row.dataset.acceptanceId || "";
    const balance = row.querySelector('[name="expected_balance"]');
    const version = row.querySelector('[name="version"]');
    if (!row.dataset.posted && !values.doc_no && !values.sku && !values.original_amount) continue;
    if (acceptanceId) values.acceptance_id = acceptanceId;
    if (balance) values.expected_balance = balance.value;
    if (version) values.version = version.value;
    acceptances.push(values);
  }
  const invoices = [];
  for (const row of invoiceTable.querySelectorAll("tr:not([data-posted])")) {
    const values = rowValues(row, INVOICE_FIELDS);
    if (!values.doc_no && !values.sku && !values.original_amount) continue;
    invoices.push(values);
  }
  return {acceptances, invoices};
}

async function execute() {
  if (submit.disabled) return;
  submit.disabled = true;
  feedback.textContent = "核销中";
  try {
    const payload = collect();
    const response = await fetch(POST, {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) {
      const detail = data.detail || {};
      out.textContent = `${detail.code || "ERROR"}\n${detail.message || "请求失败"}`;
      feedback.textContent = "已拒绝，原核销保持不变";
      return;
    }
    out.textContent = JSON.stringify({
      run_id: data.run_id,
      created: data.created,
      currency_totals: data.currency_totals,
      differences: data.differences,
      balanced: data.balanced,
    }, null, 2);
    feedback.textContent = data.created ? "已落账" : "复用已有批次，未重复核销";
    window.location.reload();
  } catch (error) {
    out.textContent = error.message;
    feedback.textContent = "失败，可重试";
  } finally {
    submit.disabled = false;
  }
}

submit?.addEventListener("click", execute);
