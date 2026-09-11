import type { TagSummary } from './types';
import { request } from './client';

/**
 * `GET /api/tags` — the caller's tag vocabulary with `todo_count`, ordered
 * `name ASC` (master §6.4). Tags are created implicitly by using them on a
 * todo, so there is no create call here (decision D-A3).
 */
export async function listTags(): Promise<TagSummary[]> {
  return request<TagSummary[]>('/api/tags');
}
