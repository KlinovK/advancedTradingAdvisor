// Narrow runtime interfaces keep the application independent of a testing framework.
export interface Transaction {
  get<T>(key: string): Promise<T | undefined>;
  put<T>(key: string, value: T): Promise<void>;
}

export interface CoordinatorStorage {
  transaction<T>(
    callback: (transaction: Transaction) => Promise<T>,
  ): Promise<T>;
}

export interface CoordinatorState {
  storage: CoordinatorStorage;
}

export interface CoordinatorStub {
  fetch(request: Request): Promise<Response>;
}

export interface CoordinatorNamespace {
  getByName(name: string): CoordinatorStub;
}

export interface Env {
  SNAPSHOT_COORDINATOR: CoordinatorNamespace;
  TELEGRAM_WEBHOOK_SECRET: string;
  GITHUB_DISPATCH_TOKEN: string;
  GITHUB_OWNER: string;
  GITHUB_REPOSITORY: string;
  GITHUB_WORKFLOW: string;
  ALLOWED_TELEGRAM_USER_ID: string;
  ALLOWED_TELEGRAM_CHAT_ID: string;
  COOLDOWN_SECONDS: string;
}
