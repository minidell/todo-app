import type { Todo, TodoPatch } from '../api/types';
import SubtaskList from './SubtaskList';
import TodoEditForm from './TodoEditForm';

interface TodoDetailProps {
  todo: Todo;
  busyIds: Set<string>;
  onSave: (patch: TodoPatch) => Promise<void>;
  onCancel: () => void;
  onAddSubtask: (parentId: string, title: string) => Promise<void>;
  onToggleSubtask: (parentId: string, id: string, completed: boolean) => Promise<void>;
  onDeleteSubtask: (parentId: string, id: string) => Promise<void>;
}

/**
 * The inline edit panel shown below a row — a group, not a modal, so the page
 * keeps its reading order and nothing has to trap focus (slice-3 spec F5).
 */
export default function TodoDetail({
  todo,
  busyIds,
  onSave,
  onCancel,
  onAddSubtask,
  onToggleSubtask,
  onDeleteSubtask,
}: TodoDetailProps) {
  const headingId = `subtasks-heading-${todo.id}`;

  return (
    <div
      role="group"
      aria-label={`Edit ${todo.title}`}
      className="mt-3 rounded-lg border border-slate-200 bg-slate-50 p-3"
    >
      <TodoEditForm todo={todo} onSave={onSave} onCancel={onCancel} />

      <section aria-labelledby={headingId} className="mt-4 border-t border-slate-200 pt-3">
        <h3 id={headingId} className="text-sm font-semibold text-slate-900">
          Subtasks
        </h3>
        <SubtaskList
          parentId={todo.id}
          subtasks={todo.subtasks}
          busyIds={busyIds}
          onToggle={onToggleSubtask}
          onDelete={onDeleteSubtask}
          onAdd={onAddSubtask}
        />
      </section>
    </div>
  );
}
