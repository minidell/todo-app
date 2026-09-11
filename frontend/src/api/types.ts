export type Priority = 'low' | 'medium' | 'high';

export interface User {
  id: string;
  email: string;
  display_name: string | null;
  created_at: string; // ISO-8601 UTC, ends with "Z"
}

export interface TokenResponse {
  access_token: string;
  token_type: string; // "bearer"
  expires_in: number; // seconds
  user: User;
}

/** Wire shape of `ListResponse` (master spec §3.3). */
export interface ListSummary {
  id: string;
  name: string;
  is_default: boolean;
  todo_count: number; // top-level todos only
  active_count: number; // top-level todos only
  created_at: string;
  updated_at: string;
}

export interface Todo {
  id: string;
  list_id: string;
  parent_id: string | null;
  title: string;
  description: string | null;
  completed: boolean;
  completed_at: string | null;
  priority: Priority;
  due_date: string | null; // "YYYY-MM-DD"
  tags: string[];
  subtasks: Todo[];
  created_at: string; // ISO-8601 UTC, ends with "Z"
  updated_at: string;
}

/** Wire shape of `TagResponse` (master spec §3.3). */
export interface TagSummary {
  id: string;
  name: string;
  todo_count: number;
}

export type StatusFilter = 'all' | 'active' | 'completed';
export type DueFilter = 'any' | 'overdue' | 'today' | 'week' | 'none';
export type TodoSort = 'created_at' | 'updated_at' | 'due_date' | 'priority' | 'title';
export type SortOrder = 'asc' | 'desc';

/**
 * Query parameters of `GET /api/todos` (master §6.3). Repeatable parameters are
 * arrays; empty arrays and the documented defaults are omitted from the URL.
 */
export interface TodoQuery {
  list_id?: string;
  status?: StatusFilter;
  priority?: Priority[];
  tag?: string[];
  due?: DueFilter;
  due_from?: string;
  due_to?: string;
  /** The caller's *local* date; drives the `due` presets. */
  today?: string;
  q?: string;
  sort?: TodoSort;
  order?: SortOrder;
  limit?: number;
  offset?: number;
}

/** A page of todos plus the unpaginated match count from `X-Total-Count`. */
export interface TodoPage {
  todos: Todo[];
  total: number;
}

/** Body of `POST /api/todos` — omitted fields take the server defaults. */
export interface TodoDraft {
  title: string;
  list_id?: string;
  description?: string | null;
  priority?: Priority;
  due_date?: string | null;
  tags?: string[];
  parent_id?: string;
}

/** Body of `POST /api/todos/{id}/subtasks` (no `list_id` / `parent_id`). */
export type SubtaskDraft = Omit<TodoDraft, 'list_id' | 'parent_id'>;

/**
 * Body of the partial `PATCH /api/todos/{id}` (master §6.3, slice 3). Only the
 * keys present are updated; an explicit `null` clears the field.
 */
export interface TodoPatch {
  title?: string;
  description?: string | null;
  completed?: boolean;
  priority?: Priority;
  due_date?: string | null;
  tags?: string[];
  list_id?: string;
  parent_id?: string | null;
}
