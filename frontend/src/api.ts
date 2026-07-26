// Every caller appends "/path", so a configured trailing slash would request
// "//path" and 404. Normalize once, here, exactly as backend config.py does.
export const serviceBaseUrl = (value: string | undefined, fallback: string): string =>
  (value ?? fallback).replace(/\/+$/, "");

const AUTHORITY = serviceBaseUrl(
  import.meta.env.VITE_AUTHORITY_URL,
  "http://localhost:8001",
);
const AGENT = serviceBaseUrl(import.meta.env.VITE_AGENT_URL, "http://localhost:8002");
const EXECUTOR = serviceBaseUrl(
  import.meta.env.VITE_EXECUTOR_URL,
  "http://localhost:8003",
);

export interface ServiceHealth {
  authority: boolean;
  agent: boolean;
  executor: boolean;
}

async function isHealthy(url: string, signal?: AbortSignal): Promise<boolean> {
  try {
    const response = await fetch(url, { signal });
    return response.ok;
  } catch {
    signal?.throwIfAborted();
    return false;
  }
}

export const api = {
  health: async (signal?: AbortSignal): Promise<ServiceHealth> => {
    const [authority, agent, executor] = await Promise.all([
      isHealthy(`${AUTHORITY}/health`, signal),
      isHealthy(`${AGENT}/health`, signal),
      isHealthy(`${EXECUTOR}/health`, signal),
    ]);
    return { authority, agent, executor };
  },
};
