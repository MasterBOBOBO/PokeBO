// 網頁版分析引擎：在 Web Worker 裡用 Pyodide 執行 lib/*.py（和本機版同一套程式）。
// - 網路：同步 XMLHttpRequest（Worker 內允許），FinMind token 走網址參數
// - 快取：data/ 掛載 IndexedDB（IDBFS），第二次開啟不用重抓歷史資料
// - 訊息：{cmd: "analyze"|"fx", id, symbol, refresh, token} → {id, ok, result|error} / {type: "progress", text}

const PYODIDE = "https://cdn.jsdelivr.net/pyodide/v0.27.7/full/";
// __init__.py 由 worker 自己產生：GitHub Pages 的 Jekyll 會隱藏底線開頭的檔案（回 404）
const MODULES = ["data", "ledger", "report", "risk", "ta", "ta_plus", "us_chips", "scenario", "reconcile", "backup", "fundamentals"];
const ROOT = "/invest";
let py = null, ready = null;

const progress = text => postMessage({type: "progress", text});

async function boot() {
  progress("步驟 1/3：載入 Python 執行環境（第一次約 10MB，之後會快取）…");
  try { importScripts(PYODIDE + "pyodide.js"); }
  catch (e) { throw new Error("無法從 CDN（cdn.jsdelivr.net）下載 Python 執行環境，請確認網路或公司防火牆後重試"); }
  py = await loadPyodide({indexURL: PYODIDE});
  const FS = py.FS;
  for (const d of [ROOT, ROOT + "/lib", ROOT + "/data"]) try { FS.mkdir(d); } catch (e) {}
  FS.mount(py.FS.filesystems.IDBFS, {}, ROOT + "/data");
  await new Promise(r => FS.syncfs(true, r));                // IndexedDB → 記憶體
  progress("步驟 2/3：載入分析程式…");
  const base = new URL("../", self.location.href);            // invest-tracker/
  const get = async path => {
    const r = await fetch(new URL(path, base), {cache: "no-cache"});   // 每次向伺服器確認版本（沒變只回 304）
    if (!r.ok) throw new Error(`下載 ${path} 失敗（HTTP ${r.status}）`);
    return [path, await r.text()];
  };
  const files = await Promise.all([...MODULES.map(m => get(`lib/${m}.py`)), get("config.example.json")]);
  for (const [path, text] of files) FS.writeFile(`${ROOT}/${path}`, text);
  FS.writeFile(`${ROOT}/lib/__init__.py`, "");
  py.runPython(`
import sys, json, urllib.error
sys.path.insert(0, "${ROOT}")
from js import XMLHttpRequest
from lib import data

def _xhr(method, url, body=None, headers=None, timeout=30):
    x = XMLHttpRequest.new()
    x.open(method, url, False)
    for k, v in (headers or {}).items():
        if k.lower() not in ("user-agent", "accept-encoding"):    # 瀏覽器不允許自訂
            x.setRequestHeader(k, v)
    x.send(body.decode() if isinstance(body, (bytes, bytearray)) else body)
    if x.status == 0 or x.status >= 400:
        raise urllib.error.HTTPError(url, x.status or 599, x.statusText or "network error", None, None)
    return x.responseText.encode("utf-8")

data.WEB = True
data.set_transport(_xhr)
`);
  postMessage({type: "ready"});                               // 主畫面據此把逾時從「啟動」切換成「分析」
}

function run(code) {
  const out = py.runPython(code);
  return JSON.parse(out);
}

self.onmessage = async ({data: msg}) => {
  try {
    const first = !ready;
    ready = ready || boot();
    try { await ready; } catch (e) { ready = null; throw e; }   // 啟動失敗時允許下次重試，不要永遠卡在失敗的 Promise
    py.globals.set("TOKEN", msg.token || "");
    py.runPython(`import os; os.environ["FINMIND_TOKEN"] = TOKEN`);
    let result;
    if (msg.cmd === "analyze") {
      progress(`${first ? "步驟 3/3：" : ""}抓取 ${msg.symbol} 資料並計算指標…`);
      py.globals.set("SYM", msg.symbol);
      py.globals.set("REFRESH", !!msg.refresh);
      result = run(`
from datetime import date, timedelta
from lib import data, ta
if not SYM[:1].isdigit():   # 美股需要匯率資料（匯率影響、成本換算）
    data.update_fx((date.today() - timedelta(days=800)).isoformat())
json.dumps(ta.analyze(SYM, refresh=REFRESH), ensure_ascii=False)`);
    } else if (msg.cmd === "fx") {
      result = run(`
from datetime import date, timedelta
from lib import data
data.update_fx((date.today() - timedelta(days=800)).isoformat())
rows = data.load_series(data.FX_PATH, "spot_buy")[-120:]
json.dumps({"date": [d for d, _ in rows], "rate": [v for _, v in rows]})`);
    }
    await new Promise(r => py.FS.syncfs(false, r));           // 記憶體 → IndexedDB
    progress("");
    postMessage({id: msg.id, ok: true, result});
  } catch (e) {
    progress("");
    const text = String(e && e.message || e);
    // Python 例外只回傳最後一行，避免把整段 traceback 丟給使用者
    const last = text.trim().split("\n").filter(Boolean).pop();
    postMessage({id: msg.id, ok: false, error: last});
  }
};
