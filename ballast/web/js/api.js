/* JSON API client. Every state-changing request sends the custom header the server requires. */

export class ApiError extends Error {
  constructor(status, body) {
    const err = (body && body.error) || {};
    super(err.title || `Request failed (${status})`);
    this.status = status;
    this.title = err.title || `Request failed (${status})`;
    this.detail = err.detail || "";
    this.fix = err.fix || "";
    this.body = body;
  }
}

async function request(method, path, body) {
  const headers = { Accept: "application/json" };
  const init = { method, headers, credentials: "same-origin" };
  if (method !== "GET") headers["X-Ballast-Request"] = "1";
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, init);
  } catch (e) {
    throw new ApiError(0, { error: { title: "Cannot reach the server", detail: String(e.message || e), fix: "Check that Ballast is running (ballastctl status) and that this address is correct." } });
  }
  if (response.status === 401) {
    window.location.href = "auth";
    throw new ApiError(401, { error: { title: "Access token required" } });
  }
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (e) { data = { error: { title: `Unexpected response (${response.status})`, detail: text.slice(0, 200) } }; }
  if (!response.ok) throw new ApiError(response.status, data);
  return data;
}

export const api = {
  get: (p) => request("GET", p),
  post: (p, b) => request("POST", p, b === undefined ? {} : b),
  put: (p, b) => request("PUT", p, b),
  del: (p) => request("DELETE", p),
};
