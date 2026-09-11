import type { Todo } from '../api/types';
import TodoItem from './TodoItem';
import type { TodoRowHandlers } from './TodoItem';

interface TodoListProps extends TodoRowHandlers {
  todos: Todo[];
  /** The browser's local date, used for the Overdue/Today copy. */
  today: string;
  busyIds: Set<string>;
}

/**
 * The list is named so it can be told apart from the other lists on the page —
 * tag chips, subtask lists, AI suggestions — both by a screen reader and by a
 * test that needs to count *todo* rows only.
 */
export const TODO_LIST_LABEL = 'Todos';

export default function TodoList({ todos, today, busyIds, ...handlers }: TodoListProps) {
  return (
    <ul aria-label={TODO_LIST_LABEL} className="flex flex-col gap-2">
      {todos.map((todo) => (
        <TodoItem
          key={todo.id}
          todo={todo}
          today={today}
          busyIds={busyIds}
          {...handlers}
        />
      ))}
    </ul>
  );
}
