export interface CodexBrokerClientOptions {
  baseUrl: string;
  internalKey?: string;
  fetchImpl?: typeof fetch;
}

export interface AuthSelection {
  profile?: string;
  authPrincipalId?: string;
}

export interface AuthScope {
  ownerHash: string;
  authPrincipalHash: string;
  sharedAuthPrincipal: boolean;
}

export interface ModelListOptions extends AuthSelection {
  cursor?: string;
  limit?: number;
  includeHidden?: boolean;
}

export interface ReasoningEffortOption {
  reasoningEffort: string;
  description: string;
}

export interface ModelServiceTier {
  id: string;
  name: string;
  description: string;
}

export interface CodexModel extends Record<string, unknown> {
  /** Stable catalog preset identifier. */
  id: string;
  /** Model slug to pass as codexOptions.model. */
  model: string;
  displayName: string;
  description: string;
  hidden: boolean;
  supportedReasoningEfforts: ReasoningEffortOption[];
  defaultReasoningEffort: string;
  inputModalities: string[];
  supportsPersonality: boolean;
  serviceTiers: ModelServiceTier[];
  defaultServiceTier: string | null;
  isDefault: boolean;
  upgrade?: string | null;
  upgradeInfo?: Record<string, unknown> | null;
}

export interface ModelListResponse extends AuthScope {
  profile: string;
  models: CodexModel[];
  nextCursor: string | null;
}

export interface AuthProfile {
  profile: string;
  state: string;
  authType: string | null;
  authFingerprint: string | null;
  createdAt: string;
  updatedAt: string;
}

export interface AuthProfileList extends AuthScope {
  profiles: AuthProfile[];
}

export interface ThreadCreateRequest extends Record<string, unknown> {
  threadId?: string;
  authPrincipalId?: string;
  profile?: string;
}

export interface TurnStartRequest extends Record<string, unknown> {
  input: Array<Record<string, unknown>>;
  authPrincipalId?: string;
  profile?: string;
  codexOptions?: CodexOptions;
}

export interface CodexOptions extends Record<string, unknown> {
  model?: string;
  effort?: string;
  reasoningEffort?: string;
  serviceTier?: string;
}

export interface BrokerThread {
  threadId: string;
  codexThreadId: string | null;
  authPrincipalHash: string;
  profile: string;
  configProfile: string;
  hostApp: string | null;
  bundleId: string | null;
  cwd: string | null;
  status: string;
  createdAt: string;
  updatedAt: string;
}

export interface TurnExecution {
  requestFingerprint: string | null;
  bundleDigest: string | null;
  resolvedOptions: Record<string, unknown> | null;
  brokerVersion: string | null;
}

export interface BrokerTurn {
  threadId: string;
  turnId: string;
  codexTurnId: string | null;
  authPrincipalHash: string;
  profile: string;
  configProfile: string;
  hostApp: string | null;
  bundleId: string | null;
  cwd: string | null;
  mode: "reject" | "queue" | "steer";
  productCorrelationId: string | null;
  status: "starting" | "queued" | "running" | "completed" | "failed" | "timed_out" | "interrupted";
  error: string | null;
  errorCode: string | null;
  publicMessage: string | null;
  adminMessage: string | null;
  createdAt: string;
  startedAt: string | null;
  completedAt: string | null;
  updatedAt: string;
  streamUrl?: string;
  execution: TurnExecution;
}

export interface BrokerInteraction {
  interactionId: string;
  threadId: string;
  turnId: string;
  kind: string;
  method: string;
  status: string;
  request: Record<string, unknown>;
  response: Record<string, unknown> | null;
  createdAt: string;
  updatedAt: string;
}

export interface InteractionList {
  ownerHash: string;
  threadId: string;
  turnId?: string;
  interactions: BrokerInteraction[];
}

export interface AuditLogList {
  ownerHash: string;
  auditLogs: Array<{
    id: number;
    ownerHash: string;
    authPrincipalHash: string;
    profile: string | null;
    threadId: string | null;
    turnId: string | null;
    action: string;
    payload: Record<string, unknown>;
    createdAt: string;
  }>;
}

export interface AccountUsageResponse extends AuthScope {
  profile: string;
  usage: Record<string, unknown>;
}

export interface AccountRateLimitsResponse extends AuthScope {
  profile: string;
  rateLimits: Record<string, unknown>;
}

export interface RateLimitResetCreditConsumeResponse extends AuthScope {
  profile: string;
  resetCredit: Record<string, unknown>;
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

  listAuthProfiles(ownerId: string, authPrincipalId?: string): Promise<AuthProfileList> {
    return this.request("GET", `/v1/owners/${enc(ownerId)}/auth/profiles${queryString({ authPrincipalId })}`);
  }

  authStatus(ownerId: string, selection: AuthSelection = {}): Promise<Record<string, unknown>> {
    return this.request("GET", `/v1/owners/${enc(ownerId)}/auth/status${authSelectionQuery(selection)}`);
  }

  listModels(ownerId: string, options: ModelListOptions = {}): Promise<ModelListResponse> {
    return this.request(
      "GET",
      `/v1/owners/${enc(ownerId)}/auth/models${queryString({
        profile: options.profile ?? "default",
        authPrincipalId: options.authPrincipalId,
        cursor: options.cursor,
        limit: options.limit,
        includeHidden: options.includeHidden ? true : undefined,
      })}`,
    );
  }

  accountUsage(ownerId: string, selection: AuthSelection = {}): Promise<AccountUsageResponse> {
    return this.request("GET", `/v1/owners/${enc(ownerId)}/auth/usage${authSelectionQuery(selection)}`);
  }

  accountRateLimits(ownerId: string, selection: AuthSelection = {}): Promise<AccountRateLimitsResponse> {
    return this.request("GET", `/v1/owners/${enc(ownerId)}/auth/rate-limits${authSelectionQuery(selection)}`);
  }

  consumeRateLimitResetCredit(
    ownerId: string,
    idempotencyKey: string,
    selection: AuthSelection = {},
  ): Promise<RateLimitResetCreditConsumeResponse> {
    return this.request("POST", `/v1/owners/${enc(ownerId)}/auth/rate-limit-reset-credit/consume`, {
      ...authSelectionBody(selection),
      idempotencyKey,
    });
  }

  probeAuth(ownerId: string, selection: AuthSelection = {}): Promise<Record<string, unknown>> {
    return this.request("POST", `/v1/owners/${enc(ownerId)}/auth/probe`, authSelectionBody(selection));
  }

  startDeviceAuth(ownerId: string, selection: AuthSelection = {}): Promise<Record<string, unknown>> {
    return this.request("POST", `/v1/owners/${enc(ownerId)}/auth/device/start`, authSelectionBody(selection));
  }

  submitDeviceCode(
    ownerId: string,
    code: string,
    selection: AuthSelection = {},
    sessionId?: string,
  ): Promise<Record<string, unknown>> {
    const body: Record<string, unknown> = { code, ...authSelectionBody(selection) };
    if (sessionId) body.sessionId = sessionId;
    return this.request("POST", `/v1/owners/${enc(ownerId)}/auth/device/submit`, body);
  }

  loginApiKey(ownerId: string, apiKey: string, selection: AuthSelection = {}): Promise<Record<string, unknown>> {
    return this.request("POST", `/v1/owners/${enc(ownerId)}/auth/api-key`, { apiKey, ...authSelectionBody(selection) });
  }

  invalidateAuthRuntime(ownerId: string, selection: AuthSelection = {}): Promise<Record<string, unknown>> {
    return this.request("POST", `/v1/owners/${enc(ownerId)}/auth/runtime/invalidate`, authSelectionBody(selection));
  }

  logout(ownerId: string, selection: AuthSelection = {}, deleteProfile = false): Promise<Record<string, unknown>> {
    return this.request("POST", `/v1/owners/${enc(ownerId)}/auth/logout`, {
      ...authSelectionBody(selection),
      deleteProfile,
    });
  }

  listAuditLogs(ownerId: string, query: Record<string, string | number | undefined> = {}): Promise<AuditLogList> {
    return this.request("GET", `/v1/owners/${enc(ownerId)}/audit-logs${queryString(query)}`);
  }

  createThread(ownerId: string, body: ThreadCreateRequest = {}): Promise<BrokerThread> {
    return this.request("POST", `/v1/owners/${enc(ownerId)}/threads`, body);
  }

  getThread(ownerId: string, threadId: string): Promise<BrokerThread> {
    return this.request("GET", `/v1/owners/${enc(ownerId)}/threads/${enc(threadId)}`);
  }

  archiveThread(ownerId: string, threadId: string): Promise<BrokerThread> {
    return this.request("POST", `/v1/owners/${enc(ownerId)}/threads/${enc(threadId)}/archive`, {});
  }

  startTurn(ownerId: string, threadId: string, body: TurnStartRequest): Promise<BrokerTurn> {
    return this.request("POST", `/v1/owners/${enc(ownerId)}/threads/${enc(threadId)}/turns`, body);
  }

  getTurn(ownerId: string, threadId: string, turnId: string): Promise<BrokerTurn> {
    return this.request("GET", `/v1/owners/${enc(ownerId)}/threads/${enc(threadId)}/turns/${enc(turnId)}`);
  }

  steerTurn(ownerId: string, threadId: string, turnId: string, input: Array<Record<string, unknown>>): Promise<BrokerTurn> {
    return this.request("POST", `/v1/owners/${enc(ownerId)}/threads/${enc(threadId)}/turns/${enc(turnId)}/steer`, { input });
  }

  interruptTurn(ownerId: string, threadId: string, turnId: string): Promise<BrokerTurn> {
    return this.request("POST", `/v1/owners/${enc(ownerId)}/threads/${enc(threadId)}/turns/${enc(turnId)}/interrupt`, {});
  }

  listInteractions(ownerId: string, threadId: string, query: Record<string, string | number | undefined> = {}): Promise<InteractionList> {
    return this.request("GET", `/v1/owners/${enc(ownerId)}/threads/${enc(threadId)}/interactions${queryString(query)}`);
  }

  resolveInteraction(
    ownerId: string,
    threadId: string,
    turnId: string,
    interactionId: string,
    body: Record<string, unknown>,
  ): Promise<BrokerInteraction> {
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

  private async request<T = Record<string, unknown>>(
    method: string,
    path: string,
    body?: Record<string, unknown>,
  ): Promise<T> {
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
    return JSON.parse(text) as T;
  }
}

function enc(value: string): string {
  return encodeURIComponent(value);
}

function queryString(query: Record<string, string | number | boolean | undefined>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined) params.set(key, String(value));
  }
  const text = params.toString();
  return text ? `?${text}` : "";
}

function authSelectionBody(selection: AuthSelection): Record<string, string> {
  const body: Record<string, string> = { profile: selection.profile ?? "default" };
  if (selection.authPrincipalId) body.authPrincipalId = selection.authPrincipalId;
  return body;
}

function authSelectionQuery(selection: AuthSelection): string {
  return queryString(authSelectionBody(selection));
}
