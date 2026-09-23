export function languageName(path: string) {
  const extension = path.split(".").pop()?.toLowerCase();
  return ({ md: "Markdown", py: "Python", js: "JavaScript", jsx: "JavaScript", ts: "TypeScript", tsx: "TypeScript", json: "JSON", yaml: "YAML", yml: "YAML", sh: "Shell" } as Record<string, string>)[extension || ""] || "Plain Text";
}

