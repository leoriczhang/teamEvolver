import { useMemo } from "react";
import CodeMirror, { EditorView } from "@uiw/react-codemirror";
import { markdown } from "@codemirror/lang-markdown";
import { python } from "@codemirror/lang-python";
import { javascript } from "@codemirror/lang-javascript";
import { json } from "@codemirror/lang-json";
import { yaml } from "@codemirror/lang-yaml";

import { languageName } from "./editorLanguage";

const theme = EditorView.theme({
  "&": { height: "100%", fontSize: "12px", backgroundColor: "var(--wb-editor)", color: "var(--foreground)" },
  ".cm-scroller": { overflow: "auto", fontFamily: "'Cascadia Code', 'SFMono-Regular', Consolas, monospace", lineHeight: "1.85" },
  ".cm-content": { padding: "16px 0" },
  ".cm-gutters": { backgroundColor: "var(--wb-editor)", color: "#969da9", border: "none", paddingRight: "12px", minWidth: "48px" },
  ".cm-activeLine, .cm-activeLineGutter": { backgroundColor: "var(--wb-hover)" },
  ".cm-cursor": { borderLeftColor: "var(--foreground)" },
  "&.cm-focused": { outline: "none" },
  ".cm-panels": { backgroundColor: "var(--wb-sidebar)", color: "var(--foreground)" },
});

export default function CodeEditor({ path, value, onChange, onCursor }: {
  path: string; value: string; onChange: (value: string) => void;
  onCursor: (position: { line: number; column: number }) => void;
}) {
  const extensions = useMemo(() => {
    const name = languageName(path);
    const language = name === "Markdown" ? markdown() : name === "Python" ? python()
      : name === "JavaScript" || name === "TypeScript" ? javascript({ typescript: name === "TypeScript", jsx: /[jt]sx$/.test(path) })
      : name === "JSON" ? json() : name === "YAML" ? yaml() : [];
    return [theme, language, EditorView.lineWrapping, EditorView.contentAttributes.of({ "aria-label": `代码编辑器 ${path}` })];
  }, [path]);
  return <CodeMirror
    className="wb-code-editor"
    value={value}
    height="100%"
    extensions={extensions}
    onChange={onChange}
    basicSetup={{ lineNumbers: true, foldGutter: true, highlightActiveLine: true, searchKeymap: true }}
    onUpdate={(update) => {
      if (!update.selectionSet && !update.docChanged) return;
      const head = update.state.selection.main.head;
      const line = update.state.doc.lineAt(head);
      onCursor({ line: line.number, column: head - line.from + 1 });
    }}
  />;
}
