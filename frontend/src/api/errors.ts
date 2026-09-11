export class ApiError extends Error {
  status: number;
  code: string | null;
  detail: string;

  constructor(status: number, code: string | null, detail: string) {
    super(`${status} ${code ?? 'error'}: ${detail}`);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

export async function toApiError(response: Response): Promise<ApiError> {
  let detail: string;
  let code: string | null = null;

  try {
    const json = await response.json();
    // Handle both array detail (validation errors) and object detail
    if (json && typeof json === 'object') {
      detail = typeof json.detail === 'string' ? json.detail : response.statusText;
      code = json.code ?? null;
    } else {
      detail = response.statusText;
    }
  } catch {
    // Non-JSON response; use status text
    detail = response.statusText;
  }

  return new ApiError(response.status, code, detail);
}
