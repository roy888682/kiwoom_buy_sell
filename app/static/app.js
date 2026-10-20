"use strict";

const $ = (id) => document.getElementById(id);
let state = null;
let settingsDirty = false;
let initialized = false;
let refreshing = false;
let toastTimer;
let dialogAction = null;

function element(tag, className, value) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (value !== undefined && value !== null) node.textContent = String(value);
  return node;
}

function numeric(value, fallback = 0) {
  const result = Number(value);
  return Number.isFinite(result) ? result : fallback;
}

function money(value, currency = "KRW") {
  if (value === undefined || value === null || value === "") return "—";
  const amount = Number(value);
  if (!Number.isFinite(amount)) return "—";
  return new Intl.NumberFormat("ko-KR", {
    style: "currency", currency,
    maximumFractionDigits: currency === "USD" ? 2 : 0,
    minimumFractionDigits: currency === "USD" ? 2 : 0,
  }).format(amount);
}

function number(value) {
  return new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 5 }).format(numeric(value));
}

function dateTime(value, timeOnly = false) {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return new Intl.DateTimeFormat("ko-KR", {timeZone: "Asia/Seoul", ...(timeOnly
    ? { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }
    : { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false })}).format(parsed);
}

function toast(message, isError = false) {
  clearTimeout(toastTimer);
  $("toast").textContent = message;
  $("toast").className = "toast" + (isError ? " error" : "");
  $("toast").hidden = false;
  toastTimer = setTimeout(() => { $("toast").hidden = true; }, isError ? 8000 : 4500);
}

function errorMessage(payload, status) {
  const detail = payload?.detail ?? payload?.message;
  if (Array.isArray(detail)) return detail.map((entry) => `${(entry.loc || []).slice(1).join(".")}: ${entry.msg || "입력값을 확인하세요."}`).join("\n");
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object") return JSON.stringify(detail);
  return `요청에 실패했습니다 (${status}). 연결 상태를 확인하세요.`;
}

async function request(path, method = "GET", body, raw = false) {
  const options = { method, headers: {}, credentials: "same-origin" };
  if (body !== undefined) {
    options.headers["Content-Type"] = raw ? "text/csv; charset=utf-8" : "application/json";
    options.body = raw ? body : JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const text = await response.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { data = { message: text }; }
  if (!response.ok) throw new Error(errorMessage(data, response.status));
  return data;
}

async function action(button, task) {
  const disabled = button.disabled;
  button.disabled = true;
  try { await task(); }
  catch (error) { toast(error.message || "요청 처리 중 오류가 발생했습니다.", true); }
  finally { button.disabled = disabled; if (state) renderControls(); }
}

function fillSettings(settings = {}) {
  const form = $("settings-form");
  for (const field of form.elements) {
    if (!field.name || settings[field.name] === undefined) continue;
    if (field.type === "checkbox") field.checked = Boolean(settings[field.name]);
    else field.value = settings[field.name];
  }
  settingsDirty = false;
  $("save-hint").textContent = "변경 후 저장하면 다음 판단부터 적용됩니다.";
}

function renderControls() {
  if (!state) return;
  const running = Boolean(state.running);
  const live = state.mode === "live";
  $("engine-status").textContent = `${live ? "키움 연결 거래" : "모의 거래"} · ${running ? "감시 중" : "대기"}`;
  $("engine-status").className = "status-pill" + (running ? " running" : "") + (live ? " live" : "");
  $("start-engine").hidden = running;
  $("stop-engine").hidden = !running;
  $("trade-mode").disabled = running;
  if (running) $("trade-mode").value = state.mode || "paper";
  $("live-notice").hidden = $("trade-mode").value !== "live";
  $("demo-scenario").disabled = live;
  $("account-sync-row").hidden = !live || running;
  const scanner = state.scanner || {};
  const scanning = scanner.status === "running";
  $("demo-reset").disabled = running || scanning;
  $("demo-reset-hint").textContent = running
    ? "감시 중에는 샘플을 초기화할 수 없습니다. 위쪽 ‘감시 중지’를 누른 뒤 실행하세요."
    : scanning
      ? "시장 탐색 중에는 샘플을 초기화할 수 없습니다. ‘탐색 중지’를 누른 뒤 실행하세요."
      : "샘플 초기화는 모의 현금·보유·주문과 예제 종목을 초기화합니다. 사용자 종목 이력은 유지됩니다.";
  $("scan-start").disabled = scanning;
  $("scan-stop").hidden = !scanning;
  $("scan-market").disabled = scanning;
}

function renderSummary() {
  const cash = state.cash || {};
  $("cash-krw").textContent = money(cash.KRW, "KRW");
  $("cash-usd").textContent = money(cash.USD, "USD");
  $("position-count").textContent = (state.positions || []).length;
  $("max-position-count").textContent = ` / ${state.settings?.max_positions ?? "—"} 종목`;
  const settings = state.settings || {};
  const breakout = numeric(settings.breakout_percent);
  $("summary-breakout").textContent = (breakout > 0 ? "+" : "") + number(breakout) + "%";
  $("summary-allocation").textContent = number(settings.allocation_percent) + "%";
  $("summary-stop").textContent = "−" + number(settings.stop_loss_percent) + "%";
  const broker = state.broker || {};
  $("connection-label").textContent = broker.configured ? "키움 연결 설정됨" : "모의 환경";
  $("connection-message").textContent = broker.message || "실제 API 연결에는 환경 변수 설정이 필요합니다.";
  $("connection-dot").className = "connection-dot" + (broker.configured ? " connected" : "");
  $("market-support-note").textContent = broker.message || "API 키와 계좌 연결, 상장 이후 수정주가 이력이 필요합니다. 모의 가격은 실시간 시세가 아닙니다.";
  const latestError = state.latest_error;
  $("global-message").hidden = !latestError;
  $("global-message").className = "notice error";
  $("global-message").textContent = latestError || "";
}

function renderScanner() {
  const scanner = state.scanner || {};
  const labels = { idle: "이력 조회 대기", running: "이력 조회 중", completed: "이력 조회 완료", stopped: "중지됨", error: "확인 필요" };
  $("scanner-status").textContent = labels[scanner.status] || scanner.status || "대기 중";
  const total = numeric(scanner.total);
  const processed = numeric(scanner.processed);
  const candidateCount = Array.isArray(scanner.candidates) ? scanner.candidates.length : numeric(scanner.candidates);
  $("scan-count").textContent = `${number(processed)} / ${number(total)} 종목 확인`;
  $("scan-candidates").textContent = `이력 검증 ${number(candidateCount)}`;
  $("scan-progress").max = Math.max(1, total);
  $("scan-progress").value = processed;
  const failures = numeric(scanner.failed);
  $("scan-failures").hidden = failures === 0;
  $("scan-failures").textContent = `데이터 조회 실패 ${number(failures)}개 · 실패 종목을 정상 조회로 계산하지 않습니다.`;
  const coverage = scanner.coverage_message || "전체 시장 종목 목록과 상장 이후 수정주가 이력이 필요합니다. 모의 샘플은 전체 시장을 의미하지 않습니다.";
  $("coverage-note").textContent = (scanner.universe_count !== undefined ? `등록된 종목 ${number(scanner.universe_count)}개 · ` : "") + coverage + (scanner.last_error ? `\n${scanner.last_error}` : "");
  $("coverage-note").style.whiteSpace = "pre-line";
  renderFeeds();
}

function renderFeeds() {
  const feeds = state.feeds || {};
  const statuses = { connecting: "연결 중", admitting: "구독 승인 중", ready: "구독 승인 완료", error: "연결 오류", stopped: "정지", idle: "대기" };
  const container = $("feeds-content");
  container.replaceChildren();
  for (const market of ["KR", "US"]) {
    const feed = feeds[market] || {};
    const card = element("div", "feed-card" + (feed.status === "ready" ? " feed-ready" : feed.status === "error" ? " feed-error" : ""));
    const header = element("div", "feed-card-heading");
    const marketLabel = element("strong", "", market === "KR" ? "국내 주식 · ETF" : "미국 주식 · ETF");
    marketLabel.append(element("small", "", market === "KR" ? "0B" : "FE"));
    header.append(marketLabel, element("span", "feed-state", statuses[feed.status] || feed.status || "미연결"));
    card.append(header);
    const requested = numeric(feed.requested_count ?? feed.requested);
    const admitted = numeric(feed.admitted_count ?? feed.admitted);
    const counts = element("div", "feed-counts");
    counts.append(element("span", "", `승인 ${number(admitted)}`), element("span", "", `요청 ${number(requested)}`));
    card.append(counts);
    const message = feed.message || (state.mode === "paper" && !state.broker?.configured ? "현재 모의 입력 가격을 사용합니다." : "감시 시작 후 시세 구독 상태를 확인하세요.");
    card.append(element("p", "feed-message", message));
    container.append(card);
  }
}

function instrumentIdentity(item) {
  const group = element("div", "instrument");
  group.append(element("span", "instrument-icon" + (item.market === "US" ? " us" : ""), String(item.symbol || "?").slice(0, 2)));
  const description = element("div");
  description.append(element("div", "instrument-name", item.name || item.symbol));
  const meta = element("div", "instrument-meta");
  meta.append(element("span", "", item.symbol));
  meta.append(element("span", "tag", item.market === "US" ? "미국" : "국내"));
  description.append(meta);
  group.append(description);
  return group;
}

function watchAction(label, item, listener, className = "") {
  const button = element("button", "text-button " + className, label);
  button.type = "button";
  button.addEventListener("click", () => listener(item, button));
  return button;
}

function renderWatchlist() {
  const watchlist = state.watchlist || [];
  $("watch-count").textContent = state.watchlist_total > watchlist.length ? `${number(watchlist.length)} / ${number(state.watchlist_total)}` : number(watchlist.length);
  const content = $("watchlist-content");
  content.replaceChildren();
  if (!watchlist.length) {
    content.append(element("div", "empty-state", "시장 탐색을 실행하거나 감시할 종목을 추가하세요."));
    return;
  }
  for (const item of watchlist) {
    const currency = item.currency || (item.market === "US" ? "USD" : "KRW");
    const card = element("article", "watch-card");
    const top = element("div", "watch-top");
    top.append(instrumentIdentity(item));
    const quote = element("div", "quote-group");
    quote.append(element("div", "current-price", money(item.current_price, currency)));
    quote.append(element("div", "quote-caption", item.source === "demo" ? "모의 입력 가격" : "최근 관측 가격"));
    top.append(quote);
    card.append(top);
    const metrics = element("div", "watch-metrics");
    for (const [title, value] of [["역대 최고가", money(item.all_time_high, currency)], ["돌파 기준선", money(item.threshold, currency)], ["이전 관측 가격", money(item.previous_price, currency)]]) {
      const metric = element("div");
      metric.append(element("small", "", title), element("strong", "", value));
      metrics.append(metric);
    }
    card.append(metrics);
    const footer = element("div", "watch-footer");
    let historyText = item.history_verified ? `수정주가 · 전체 이력 확인 / ${item.history_through || "—"} 기준` : "이력 미확인 · 매수 판단 보류";
    if (!item.history_verified && item.history_reason) historyText = item.history_reason;
    if (item.source === "demo") historyText = "모의 샘플 · " + historyText;
    footer.append(element("span", "history-status" + (item.history_verified ? "" : " warning"), historyText));
    const tools = element("div", "watch-tools");
    if (state.mode !== "live") {
      const historyButton = watchAction("모의 이력", item, showHistoryDialog);
      historyButton.disabled = Boolean(state.running);
      historyButton.title = state.running ? "모의 이력 변경은 감시를 멈춘 후 가능합니다." : "모의 거래용 기준가 설정";
      tools.append(historyButton);
      tools.append(watchAction("가격 입력", item, showTickDialog));
    }
    tools.append(watchAction("삭제", item, removeWatch, "remove-button"));
    footer.append(tools);
    card.append(footer);
    content.append(card);
  }
}

function cell(value, className = "") { return element("td", className, value); }

function renderPositions() {
  const positions = state.positions || [];
  $("positions-content").replaceChildren();
  $("positions-empty").hidden = positions.length > 0;
  for (const item of positions) {
    const currency = item.currency || (item.market === "US" ? "USD" : "KRW");
    const row = element("tr");
    const name = cell(item.name || item.symbol);
    name.append(element("small", "", item.symbol + " · " + currency));
    row.append(name, cell(number(item.quantity), "numeric"), cell(money(item.average_price, currency), "numeric"), cell(money(item.current_price, currency), "numeric"), cell(money(item.stop_price, currency), "numeric stop-price"));
    $("positions-content").append(row);
  }
}

function renderOrders() {
  const orders = [...(state.orders || [])].sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
  $("orders-content").replaceChildren();
  $("orders-empty").hidden = orders.length > 0;
  const statuses = { filled: "체결 완료", pending: "접수 대기", submitted: "주문 접수", accepted: "주문 접수", partial: "부분 체결", partially_filled: "부분 체결", rejected: "주문 거절", canceled: "취소", cancelled: "취소", unknown: "확인 필요", expired: "만료", reserved: "예약 중" };
  for (const item of orders.slice(0, 50)) {
    const currency = item.currency || (item.market === "US" ? "USD" : "KRW");
    const row = element("tr");
    const name = cell(item.symbol);
    name.append(element("small", "", dateTime(item.created_at)));
    const side = cell("");
    const buy = String(item.side).toLowerCase() === "buy";
    side.append(element("span", "tag " + (buy ? "side-buy" : "side-sell"), buy ? "매수" : "매도"));
    const quantity = cell(`${number(item.quantity)} / ${number(item.filled_quantity)}`, "numeric");
    const price = cell(money(item.limit_price ?? item.price, currency), "numeric");
    if (numeric(item.filled_quantity) > 0 && item.average_fill_price) price.append(element("small", "", `평균 ${money(item.average_fill_price, currency)}`));
    const status = cell(statuses[item.status] || item.status || "—");
    if (item.reason) status.title = item.reason;
    if (["pending", "partial", "unknown", "submitted", "accepted", "partially_filled"].includes(item.status)) {
      const cancel = element("button", "text-button", "취소 요청");
      cancel.type = "button";
      cancel.style.display = "block";
      cancel.style.fontSize = "8px";
      cancel.addEventListener("click", () => showCancelDialog(item));
      status.append(cancel);
      if (state.mode === "live" && !state.running) {
        const bind = element("button", "text-button", "주문 번호 · 날짜 대조");
        bind.type = "button";
        bind.style.display = "block";
        bind.style.fontSize = "8px";
        bind.addEventListener("click", () => showBindDialog(item));
        status.append(bind);
      }
    }
    row.append(name, side, quantity, price, status);
    $("orders-content").append(row);
  }
}

function renderEvents() {
  const events = [...(state.events || [])].sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
  const content = $("events-content");
  content.replaceChildren();
  if (!events.length) { content.append(element("div", "empty-state", "새로운 활동을 기다립니다.")); return; }
  for (const event of events.slice(0, 35)) {
    const row = element("div", "event-row " + (["error", "warning"].includes(event.level) ? event.level : ""));
    row.append(element("time", "", dateTime(event.created_at, true)), element("span", "", event.message));
    content.append(row);
  }
}

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    state = await request("/api/state");
    if (!initialized) {
      fillSettings(state.settings);
      $("trade-mode").value = state.mode || "paper";
      initialized = true;
    } else if (!settingsDirty && document.activeElement?.form !== $("settings-form")) fillSettings(state.settings);
    renderSummary(); renderControls(); renderScanner(); renderWatchlist(); renderPositions(); renderOrders(); renderEvents();
    $("last-updated").textContent = "최근 동기화 " + dateTime(new Date().toISOString(), true);
  } catch (error) {
    $("connection-label").textContent = "연결 확인 필요";
    $("connection-message").textContent = error.message || "로컬 서버에 연결할 수 없습니다.";
    $("connection-dot").className = "connection-dot";
    $("last-updated").textContent = "연결 끊김 · 자동 재시도 중";
    if (!initialized) {
      $("global-message").hidden = false;
      $("global-message").className = "notice error";
      $("global-message").textContent = "프로그램에 연결할 수 없습니다. 서버 실행 상태를 확인하세요.";
    }
  } finally { refreshing = false; }
}

function openDialog({ title, description, fields, submitLabel = "확인", onSubmit }) {
  $("dialog-title").textContent = title;
  $("dialog-description").textContent = description;
  $("dialog-submit").textContent = submitLabel;
  $("dialog-error").textContent = "";
  const container = $("dialog-fields");
  container.replaceChildren();
  for (const field of fields) {
    const label = element("label", "dialog-field" + (field.type === "checkbox" ? " check" : ""));
    const text = element("span", "", field.label);
    const input = element("input");
    input.name = field.name;
    input.type = field.type || "text";
    if (field.value !== undefined) input.value = field.value;
    if (field.required !== false) input.required = true;
    if (field.min !== undefined) input.min = field.min;
    if (field.step !== undefined) input.step = field.step;
    if (field.placeholder) input.placeholder = field.placeholder;
    if (field.type === "checkbox") label.append(input, text);
    else label.append(text, input);
    container.append(label);
  }
  dialogAction = onSubmit;
  $("input-dialog").showModal();
}

$("dialog-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("dialog-submit");
  button.disabled = true;
  $("dialog-error").textContent = "";
  try {
    const data = Object.fromEntries(new FormData(event.currentTarget));
    await dialogAction(data);
    $("input-dialog").close();
    await refresh();
  } catch (error) { $("dialog-error").textContent = error.message; }
  finally { button.disabled = false; }
});
for (const id of ["dialog-close", "dialog-cancel"]) $(id).addEventListener("click", () => $("input-dialog").close());

function yesterday(market) {
  const date = new Intl.DateTimeFormat("en-CA", { timeZone: market === "US" ? "America/New_York" : "Asia/Seoul", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
  const day = new Date(date + "T12:00:00Z");
  do { day.setUTCDate(day.getUTCDate() - 1); } while ([0, 6].includes(day.getUTCDay()));
  return day.toISOString().slice(0, 10);
}

function showHistoryDialog(item) {
  openDialog({
    title: `${item.name || item.symbol} · 모의 이력`,
    description: "모의 거래 연습용 기준입니다. 이 화면에서 수동으로 입력한 최고가는 실제 거래의 전체 이력 검증을 대신하지 않습니다.",
    fields: [
      { name: "all_time_high", label: "전 거래일까지의 역대 최고가", type: "number", min: "0.0001", step: "any", value: item.all_time_high || "10000" },
      { name: "history_through", label: "이력 기준일", type: "date", value: item.history_through || yesterday(item.market) },
      { name: "confirmed_adjusted_complete", label: "모의 시나리오에서 상장 이후 수정주가 전체 이력을 확보한 것으로 가정합니다.", type: "checkbox" },
    ],
    submitLabel: "모의 기준 저장",
    onSubmit: async (data) => {
      await request(`/api/watchlist/${encodeURIComponent(item.key)}/history`, "POST", { all_time_high: data.all_time_high, history_through: data.history_through, confirmed_adjusted_complete: data.confirmed_adjusted_complete === "on" });
      toast("모의 최고가 기준을 저장했습니다.");
    },
  });
}

function showTickDialog(item) {
  openDialog({
    title: `${item.name || item.symbol} · 모의 가격`,
    description: "관측 가격을 한 번 전달합니다. 첫 가격은 기준값만 기록하며, 다음 관측값부터 아래→위 돌파를 판단합니다. 보유 종목은 손절 기준도 확인합니다.",
    fields: [{ name: "price", label: `관측 가격 (${item.currency || (item.market === "US" ? "USD" : "KRW")})`, type: "number", min: "0.0001", step: "any", value: item.current_price || item.threshold || item.all_time_high || "10000" }],
    submitLabel: "가격 전달",
    onSubmit: async (data) => {
      await request(`/api/watchlist/${encodeURIComponent(item.key)}/tick`, "POST", { price: data.price });
      toast("모의 가격을 전달했습니다. 주문과 활동 기록을 확인하세요.");
    },
  });
}

function removeWatch(item) {
  openDialog({
    title: "감시 종목 삭제",
    description: `${item.name || item.symbol}을 감시 목록에서 삭제합니다. 보유 종목과 주문은 삭제되지 않습니다.`,
    fields: [{ name: "confirm", type: "checkbox", label: "이 종목의 신규 매수 감시를 중단합니다." }],
    submitLabel: "감시 목록에서 삭제",
    onSubmit: async () => {
      await request(`/api/watchlist/${encodeURIComponent(item.key)}`, "DELETE");
      toast("감시 목록에서 삭제했습니다.");
    },
  });
}

function showCancelDialog(order) {
  openDialog({
    title: `${order.symbol} · 미체결 주문 취소`,
    description: "남아 있는 미체결 수량에 대해 취소를 요청합니다. 취소 요청과 체결은 동시에 발생할 수 있으므로, 최종 상태는 계좌와 주문 내역에서 확인하세요. 응답 불명 주문은 키움 계좌에서 직접 확인해야 할 수 있습니다.",
    fields: [{ name: "confirm", type: "checkbox", label: "이 주문의 남은 수량 취소를 요청합니다." }],
    submitLabel: "취소 요청",
    onSubmit: async () => {
      await request(`/api/orders/${encodeURIComponent(order.id)}/cancel`, "POST", {});
      toast("취소 요청을 처리했습니다. 최종 주문 상태를 확인하세요.");
    },
  });
}

function showBindDialog(order) {
  openDialog({
    title: `${order.symbol} · 주문 번호 · 날짜 대조`,
    description: `로컬 ${order.side === "BUY" ? "매수" : "매도"} ${number(order.quantity)}주 주문을 키움 계좌와 대조합니다. 종목·방향·수량과 접수 시각을 직접 확인하세요. 전날 주문을 복구할 때는 키움에 표시된 주문 일자도 입력하세요. 이 동작은 새 주문을 보내지 않습니다.`,
    fields: [
      { name: "broker_order_id", label: "키움 주문 번호 (숫자)", placeholder: "키움에서 확인한 주문 번호" },
      { name: "broker_order_date", label: "키움 주문 일자 (선택 · 전날 주문 대조 시 입력)", type: "date", required: false },
    ],
    submitLabel: "기존 주문과 대조",
    onSubmit: async (data) => {
      if (!/^[0-9]{1,30}$/.test(data.broker_order_id)) throw new Error("숫자로 된 키움 주문 번호를 입력하세요.");
      const body = { broker_order_id: data.broker_order_id };
      if (data.broker_order_date) body.broker_order_date = data.broker_order_date;
      await request(`/api/orders/${encodeURIComponent(order.id)}/bind`, "POST", body);
      toast("키움 주문과 대조했습니다. 체결 상태를 확인하세요.");
    },
  });
}

$("account-sync").addEventListener("click", () => action($("account-sync"), async () => {
  await request("/api/account/sync", "POST", {});
  await refresh();
  toast("실계좌와 체결 내역을 동기화했습니다.");
}));

$("settings-form").addEventListener("input", () => {
  settingsDirty = true;
  $("save-hint").textContent = "저장하지 않은 변경 사항이 있습니다.";
});

$("settings-form").addEventListener("submit", (event) => {
  event.preventDefault();
  action($("save-settings"), async () => {
    const data = Object.fromEntries(new FormData(event.currentTarget));
    const settings = {
      breakout_percent: data.breakout_percent,
      allocation_percent: data.allocation_percent,
      stop_loss_percent: data.stop_loss_percent,
      fee_reserve_percent: data.fee_reserve_percent,
      allow_pyramiding: $("allow-pyramiding").checked,
      reentry_cooldown_minutes: Number(data.reentry_cooldown_minutes),
      max_positions: Number(data.max_positions),
      poll_interval_seconds: Number(data.poll_interval_seconds),
      universe_mode: data.universe_mode,
    };
    await request("/api/settings", "PUT", settings);
    settingsDirty = false;
    await refresh();
    toast("매매 설정을 저장했습니다.");
    $("save-hint").textContent = "저장되었습니다. 다음 판단부터 적용됩니다.";
  });
});

$("trade-mode").addEventListener("change", renderControls);
$("start-engine").addEventListener("click", () => {
  if ($("trade-mode").value === "live") {
    openDialog({
      title: "키움 연결 거래 시작",
      description: "연결한 계좌의 기존 현금 주식·ETF 보유 종목도 모두 불러와 이 프로그램의 손절 규칙을 적용합니다. 첫 관측 가격이 손절선 이하이면 기존 보유분에도 전량 매도 주문이 나갈 수 있습니다. 매도 불가 수량·신용/수량 불일치 등이 있으면 시작을 차단합니다.\n\n계좌·현금·보유 종목·전체 이력 범위를 확인하세요. 활성화하려면 아래에 ‘실거래 시작’을 정확히 입력하세요. 감시 중지는 손절 감시도 멈추며, 기존 미체결 주문을 취소하지 않습니다.",
      fields: [{ name: "confirmation", label: "확인 문구", placeholder: "실거래 시작" }],
      submitLabel: "키움 감시 시작",
      onSubmit: async (data) => {
        if (data.confirmation !== "실거래 시작") throw new Error("‘실거래 시작’을 정확히 입력해 주세요.");
        await request("/api/engine/start", "POST", { mode: "live", confirmation: data.confirmation });
        toast("키움 연결 거래 감시를 시작했습니다.");
      },
    });
    return;
  }
  action($("start-engine"), async () => {
    await request("/api/engine/start", "POST", { mode: "paper", confirmation: "" });
    await refresh();
    toast("모의 거래 감시를 시작했습니다.");
  });
});

$("stop-engine").addEventListener("click", () => action($("stop-engine"), async () => {
  await request("/api/engine/stop", "POST", {});
  await refresh();
  toast("감시와 자동 손절을 중지했습니다. 기존 미체결 주문은 유지됩니다.");
}));

$("toggle-add-watch").addEventListener("click", () => {
  $("watch-form").hidden = !$("watch-form").hidden;
  if (!$("watch-form").hidden) $("watch-form").elements.symbol.focus();
});

$("watch-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  action(form.querySelector("button"), async () => {
    const data = Object.fromEntries(new FormData(form));
    data.symbol = data.symbol.trim().toUpperCase();
    data.name = data.name.trim();
    await request("/api/watchlist", "POST", data);
    form.reset();
    form.hidden = true;
    await refresh();
    toast("감시 종목을 추가했습니다. 최고가 이력이 확인되면 매수 판단에 사용됩니다.");
  });
});

$("scan-start").addEventListener("click", () => action($("scan-start"), async () => {
  const result = await request("/api/scanner/start", "POST", { market: $("scan-market").value });
  await refresh();
  toast(result.message || "시장 탐색을 시작했습니다. 실제 데이터 확보 범위를 확인하세요.");
}));

$("scan-stop").addEventListener("click", () => action($("scan-stop"), async () => {
  await request("/api/scanner/stop", "POST", {});
  await refresh();
  toast("시장 탐색을 중지했습니다.");
}));

$("universe-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  action(form.querySelector("button"), async () => {
    const file = $("universe-file").files[0];
    if ($("scan-market").value === "ALL") throw new Error("CSV를 가져올 시장을 국내 또는 미국 중에서 선택하세요.");
    if (!file) throw new Error("가져올 CSV 파일을 선택하세요.");
    if (file.size > 25 * 1024 * 1024) throw new Error("파일이 너무 큽니다. 25MB 이하의 CSV를 선택하세요.");
    const csv = await file.text();
    const result = await request(`/api/universe/import?market=${encodeURIComponent($("scan-market").value)}`, "POST", csv, true);
    form.reset();
    await refresh();
    toast(result.message || "CSV를 가져왔습니다. 시장 탐색으로 가격 이력을 검증하세요.");
  });
});

$("demo-reset").addEventListener("click", () => {
  openDialog({
    title: "모의 환경 초기화",
    description: "가상 현금, 모의 보유·주문과 두 예제 종목을 초기화합니다. 사용자가 등록한 종목 이력은 보존됩니다. 실제 계좌에는 영향을 주지 않으며, 감시와 시장 탐색을 정지한 상태에서만 가능합니다.",
    fields: [{ name: "confirm", type: "checkbox", label: "현재 모의 상태를 지우고 샘플 환경으로 되돌립니다." }],
    submitLabel: "샘플로 초기화",
    onSubmit: async () => {
      await request("/api/demo/reset", "POST", {});
      toast("모의 샘플을 준비했습니다. 감시 시작 후 돌파 시나리오를 실행하세요.");
    },
  });
});

$("demo-scenario").addEventListener("click", () => action($("demo-scenario"), async () => {
  if (!state?.running) throw new Error("먼저 모의 거래 모드에서 ‘감시 시작’을 눌러주세요.");
  const result = await request("/api/demo/scenario", "POST", {});
  await refresh();
  toast(result.message || "모의 돌파 가격을 전달했습니다. 주문 내역을 확인하세요.");
}));

for (const anchor of document.querySelectorAll(".nav-item")) {
  anchor.addEventListener("click", () => {
    for (const item of document.querySelectorAll(".nav-item")) item.classList.remove("active");
    anchor.classList.add("active");
  });
}

$("today-label").textContent = new Intl.DateTimeFormat("ko-KR", { timeZone: "Asia/Seoul", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
refresh();
setInterval(refresh, 3000);
