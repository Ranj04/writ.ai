import { useRef } from "react";

export interface CodeDocumentEditorProps {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  description?: string;
  disabled?: boolean;
  rows?: number;
}

export function CodeDocumentEditor({
  id,
  label,
  value,
  onChange,
  description,
  disabled = false,
  rows = 12,
}: CodeDocumentEditorProps) {
  const lineNumbersRef = useRef<HTMLPreElement>(null);
  const lineCount = Math.max(1, value.split("\n").length);
  const lineNumbers = Array.from(
    { length: lineCount },
    (_, index) => index + 1,
  ).join("\n");
  const descriptionId = description ? `${id}-description` : undefined;

  return (
    <div className="lw-editor-field">
      <label htmlFor={id}>{label}</label>
      {description ? (
        <p id={descriptionId}>{description}</p>
      ) : null}
      <div className="lw-code-editor">
        <pre ref={lineNumbersRef} aria-hidden="true">
          {lineNumbers}
        </pre>
        <textarea
          id={id}
          value={value}
          rows={rows}
          spellCheck={false}
          autoCapitalize="off"
          autoComplete="off"
          aria-describedby={descriptionId}
          disabled={disabled}
          onChange={(event) => onChange(event.target.value)}
          onScroll={(event) => {
            if (lineNumbersRef.current) {
              lineNumbersRef.current.scrollTop = event.currentTarget.scrollTop;
            }
          }}
        />
      </div>
    </div>
  );
}
