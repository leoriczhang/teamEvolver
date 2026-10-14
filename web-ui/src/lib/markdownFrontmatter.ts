import { yamlLanguage } from "@codemirror/lang-yaml";

/** Keep source text intact; hide only a complete YAML mapping in reading previews. */
export function markdownBodyForPreview(source: string): string {
  const opening = /^\uFEFF?---[ \t]*\r?\n/.exec(source);
  if (!opening) return source;

  const remainder = source.slice(opening[0].length);
  const closing = /^(?:---|\.\.\.)[ \t]*(?:\r?\n|$)/m.exec(remainder);
  if (!closing) return source;

  const tree = yamlLanguage.parser.parse(remainder.slice(0, closing.index));
  let hasError = false;
  tree.iterate({ enter: (node) => { if (node.type.isError) hasError = true; } });
  const documents = tree.topNode.getChildren("Document");
  if (hasError || documents.length !== 1) return source;
  const mapping = documents[0].getChild("BlockMapping") || documents[0].getChild("FlowMapping");
  // A thematic break followed by prose or a Markdown heading is not frontmatter.
  if (!mapping || !mapping.getChild("Pair")) return source;

  return remainder.slice(closing.index + closing[0].length);
}
