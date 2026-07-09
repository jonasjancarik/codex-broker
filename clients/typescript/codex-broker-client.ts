export interface CodexBrokerClientOptions {
  baseUrl: string;
  internalKey?: string;
  fetchImpl?: typeof fetch;
}

export class CodexBrokerClient {
  private readonly baseUrl: string;
  private readonly internalKey?: string;
  private readonly fetchImpl: typeof fetch;

  constructor(options: CodexBrokerClientOptions) {
    this.baseUrl = options.baseUrl.replace(/\/+$/, "");
    this.internalKey = options.internalKey;
    this.fetchImpl = options.fetchImpl ?? fetch;
  }

  authStatus(ownerId: string, profile = "default"): Promise<Record<string, unknown>> {
    return this.request("GET", `/v1/owners/${enc(ownerId)}/auth/status?profile=${enc(profile)}`);
  }

  probeAuth(ownerId: string, profile = "default"): Promise<Record<string, unknown>> {
    return this.request("POST", `/v1/owners/${enc(ownerId)}/auth/probe`, { profile });
  }

  startDeviceAuth(ownerId: string, profile = "default"): Promise<Record<string, unknown>> {
    return this.request("POST", `/v1/owners/${enc(ownerId)}/auth/device/start`, { profile });
  }

  submitDeviceCode(ownerId: string, code: string, profile = "default", sessionId?: string): Promise<Record<string, unknown>> {
    const body: Record<string, unknown> = { code, profile };
    if (sessionId) body.sessionId = sessionId;
    return this.request("POST", `/v1/owners/${enc(ownerId)}/auth/device/submit`, body);
  }

  loginApiKey(ownerId: string, apiKey: string, profile = "default"): Promise<Record<string, unknown>> {
    return this.request("POST", `/v1/owners/${enc(ownerId)}/auth/api-key`, { apiKey, profile });
  }

  logout(ownerId: string, profile = "default", deleteProfile = false): Promise<Record<string, unknown>> {
    return this.request("POST", `/v1/owners/${enc(ownerId)}/auth/logout`, {
      profile,
      deleteProfile,
    });
  }

  listAuditLogs(ownerId: string, query: Record<string, string | number | undefined> = {}): Promise<Record<string, unknown>> {
    return this.request("GET", `/v1/owners/${enc(ownerId)}/audit-logs${queryString(query)}`);
  }

  createThread(ownerId: string, body: Record<string, unknown> = {}): Promise<Record<string, unknown>> {
    return this.request("POST", `/v1/owners/${enc(ownerId)}/threads`, body);
  }

  getThread(ownerId: string, threadId: string): Promise<Record<string, unknown>> {
    return this.request("GET", `/v1/owners/${enc(ownerId)}/threads/${enc(threadId)}`);
  }

  archiveThread(ownerId: string, threadId: string): Promise<Record<string, unknown>> {
    return this.request("POST", `/v1/owners/${enc(ownerId)}/threads/${enc(threadId)}/archive`, {});
  }

  startTurn(ownerId: string, threadId: string, body: Record<string, unknown>): Promise<Record<string, unknown>> {
    return this.request("POST", `/v1/owners/${enc(ownerId)}/threads/${enc(threadId)}/turns`, body);
  }

  getTurn(ownerId: string, threadId: string, turnId: string): Promise<Record<string, unknown>> {
    return this.request("GET", `/v1/owners/${enc(ownerId)}/threads/${enc(threadId)}/turns/${enc(turnId)}`);
  }

  steerTurn(ownerId: string, threadId: string, turnId: string, input: Array<Record<string, unknown>>): Promise<Record<string, unknown>> {
    return this.request("POST", `/v1/owners/${enc(ownerId)}/threads/${enc(threadId)}/turns/${enc(turnId)}/steer`, { input });
  }

  interruptTurn(ownerId: string, threadId: string, turnId: string): Promise<Record<string, unknown>> {
    return this.request("POST", `/v1/owners/${enc(ownerId)}/threads/${enc(threadId)}/turns/${enc(turnId)}/interrupt`, {});
  }

  listInteractions(ownerId: string, threadId: string, query: Record<string, string | number | undefined> = {}): Promise<Record<string, unknown>> {
    return this.request("GET", `/v1/owners/${enc(ownerId)}/threads/${enc(threadId)}/interactions${queryString(query)}`);
  }

  resolveInteraction(
    ownerId: string,
    threadId: string,
    turnId: string,
    interactionId: string,
    body: Record<string, unknown>,
  ): Promise<Record<string, unknown>> {
    return this.request(
      "POST",
      `/v1/owners/${enc(ownerId)}/threads/${enc(threadId)}/turns/${enc(turnId)}/interactions/${enc(interactionId)}/resolve`,
      body,
    );
  }

  eventsUrl(ownerId: string, threadId: string, turnId?: string): string {
    const query = new URLSearchParams();
    if (turnId) query.set("turnId", turnId);
    const suffix = query.toString() ? `?${query.toString()}` : "";
    return `${this.baseUrl}/v1/owners/${enc(ownerId)}/threads/${enc(threadId)}/events${suffix}`;
  }

  private async request(method: string, path: string, body?: Record<string, unknown>): Promise<Record<string, unknown>> {
    const headers: Record<string, string> = { Accept: "application/json" };
    if (this.internalKey) headers.Authorization = `Bearer ${this.internalKey}`;
    if (body !== undefined) headers["Content-Type"] = "application/json";

    const response = await this.fetchImpl(`${this.baseUrl}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const text = await response.text();
    if (!response.ok) {
      throw new Error(`Codex broker ${response.status}: ${text}`);
    }
    return JSON.parse(text) as Record<string, unknown>;
  }
}

function enc(value: string): string {
  return encodeURIComponent(value);
}

function queryString(query: Record<string, string | number | undefined>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined) params.set(key, String(value));
  }
  const text = params.toString();
  return text ? `?${text}` : "";
}
