import { spawn } from "node:child_process";
import readline from "node:readline";

const child = spawn(process.execPath, ["/opt/browser-mcp/browser-proxy.mjs", `smoke-${Date.now()}`], {
  env: process.env,
  stdio: ["pipe", "pipe", "inherit"],
});
const lines = readline.createInterface({ input: child.stdout, crlfDelay: Infinity });
const pending = new Map();
let nextId = 1;

lines.on("line", (line) => {
  const message = JSON.parse(line);
  const waiter = pending.get(message.id);
  if (waiter) {
    pending.delete(message.id);
    message.error ? waiter.reject(new Error(message.error.message)) : waiter.resolve(message.result);
  }
});

function request(method, params = {}) {
  const id = nextId++;
  child.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", id, method, params })}\n`);
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      pending.delete(id);
      reject(new Error(`Timed out waiting for ${method}.`));
    }, 120_000);
    pending.set(id, {
      resolve: (value) => { clearTimeout(timeout); resolve(value); },
      reject: (error) => { clearTimeout(timeout); reject(error); },
    });
  });
}

try {
  await request("initialize", {
    protocolVersion: "2025-06-18",
    capabilities: {},
    clientInfo: { name: "markshaw-browser-smoke-test", version: "1.0.0" },
  });
  child.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized", params: {} })}\n`);
  const listed = await request("tools/list");
  const names = listed.tools.map((tool) => tool.name);
  const required = ["browser_navigate", "browser_snapshot", "browser_take_screenshot", "browser_mouse_click_xy"];
  const forbidden = ["browser_evaluate", "browser_run_code_unsafe", "browser_network_request", "browser_network_requests"];
  if (required.some((name) => !names.includes(name))) throw new Error("One or more required browser tools are missing.");
  if (forbidden.some((name) => names.includes(name))) throw new Error("One or more blocked browser tools were exposed.");

  const navigation = await request("tools/call", { name: "browser_navigate", arguments: { url: "https://example.com/" } });
  if (navigation.isError) throw new Error("Navigation failed.");
  const screenshot = await request("tools/call", { name: "browser_take_screenshot", arguments: { type: "png", scale: "css" } });
  const imageReturned = Array.isArray(screenshot.content) && screenshot.content.some((item) => item.type === "image");
  if (!imageReturned) throw new Error("The screenshot tool did not return an image.");

  let privateNetworkBlocked = false;
  try {
    const privateResult = await request("tools/call", { name: "browser_navigate", arguments: { url: "http://10.0.0.181/" } });
    const privateResultText = JSON.stringify(privateResult);
    privateNetworkBlocked = privateResult.isError === true ||
      privateResultText.includes("ERR_TUNNEL_CONNECTION_FAILED") ||
      privateResultText.includes("Local and private network destinations are blocked") ||
      privateResultText.includes("403 Forbidden");
  } catch {
    privateNetworkBlocked = true;
  }
  if (!privateNetworkBlocked) throw new Error("Private-network navigation was not blocked.");

  const youtubeNavigation = await request("tools/call", {
    name: "browser_navigate",
    arguments: { url: "https://www.youtube.com/watch?v=jNQXAC9IVRw" },
  });
  if (youtubeNavigation.isError) throw new Error("YouTube navigation failed.");
  await request("tools/call", { name: "browser_wait_for", arguments: { time: 3 } });
  const youtubeScreenshot = await request("tools/call", {
    name: "browser_take_screenshot",
    arguments: { type: "png", scale: "css" },
  });
  const youtubeFrameReturned = Array.isArray(youtubeScreenshot.content) &&
    youtubeScreenshot.content.some((item) => item.type === "image");
  if (!youtubeFrameReturned) throw new Error("The YouTube frame was not returned as an image.");

  process.stdout.write(JSON.stringify({
    mcp_connected: true,
    tools_available: names.length,
    visual_screenshot: true,
    coordinate_controls: true,
    unsafe_server_code_blocked: true,
    private_network_blocked: true,
    youtube_visual_frame: true,
  }) + "\n");
} finally {
  child.stdin.end();
  child.kill("SIGTERM");
}
