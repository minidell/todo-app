import type { ListSummary } from './types';
import { request } from './client';

/** `GET /api/lists` — ordered `created_at ASC` (master §6.2). */
export async function listLists(): Promise<ListSummary[]> {
  return request<ListSummary[]>('/api/lists');
}

/** `POST /api/lists` — 409 `list_name_taken` on a duplicate name. */
export async function createList(name: string): Promise<ListSummary> {
  return request<ListSummary>('/api/lists', 'POST', { name });
}

/** `PATCH /api/lists/{id}` — 404 `list_not_found`, 409 `list_name_taken`. */
export async function renameList(id: string, name: string): Promise<ListSummary> {
  return request<ListSummary>(
    `/api/lists/${encodeURIComponent(id)}`,
    'PATCH',
    { name }
  );
}

/** `DELETE /api/lists/{id}` — 409 `cannot_delete_last_list` for the only list. */
export async function deleteList(id: string): Promise<void> {
  return request<void>(`/api/lists/${encodeURIComponent(id)}`, 'DELETE');
}
