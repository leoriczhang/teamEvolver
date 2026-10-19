import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(scriptDir, "..");
const requireFromWebUi = createRequire(path.join(root, "web-ui", "package.json"));
const ts = requireFromWebUi("typescript");

const files = JSON.parse(fs.readFileSync(0, "utf8"));
const records = [];
const interfaces = [];
const errors = [];

function lineOf(sourceFile, node) {
  return sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile)).line + 1;
}

function nodeName(node, sourceFile) {
  if (!node) return "";
  if (ts.isIdentifier(node) || ts.isPrivateIdentifier(node)) return node.text;
  if (ts.isStringLiteral(node) || ts.isNumericLiteral(node)) return node.text;
  return node.getText(sourceFile);
}

function bindingNames(nameNode) {
  if (ts.isIdentifier(nameNode)) return [nameNode.text];
  if (ts.isObjectBindingPattern(nameNode) || ts.isArrayBindingPattern(nameNode)) {
    return nameNode.elements.flatMap((element) => {
      if (ts.isOmittedExpression(element)) return [];
      return bindingNames(element.name);
    });
  }
  return [nameNode.getText()];
}

function jsDocText(node) {
  const docs = node.jsDoc || [];
  for (const doc of docs) {
    if (typeof doc.comment === "string" && doc.comment.trim()) return doc.comment.trim();
    if (Array.isArray(doc.comment)) {
      const text = doc.comment.map((part) => part.text || "").join("").trim();
      if (text) return text;
    }
  }
  return "";
}

function functionName(node, sourceFile) {
  if (node.name) return nodeName(node.name, sourceFile);
  const parent = node.parent;
  if (parent && ts.isVariableDeclaration(parent)) {
    const names = bindingNames(parent.name);
    return names.length === 1 ? names[0] : "";
  }
  if (parent && ts.isPropertyAssignment(parent)) return nodeName(parent.name, sourceFile);
  if (parent && ts.isPropertyDeclaration(parent)) return nodeName(parent.name, sourceFile);
  if (parent && ts.isBinaryExpression(parent)) return parent.left.getText(sourceFile);
  return "";
}

function functionSignature(node, name, sourceFile) {
  const typeParameters = node.typeParameters?.length
    ? `<${node.typeParameters.map((item) => item.getText(sourceFile)).join(", ")}>`
    : "";
  const parameters = node.parameters
    .map((parameter) => parameter.getText(sourceFile))
    .join(", ");
  const returnType = node.type ? `: ${node.type.getText(sourceFile)}` : "";
  return `${name}${typeParameters}(${parameters})${returnType}`;
}

function declarationType(node, sourceFile) {
  if (node.type) return node.type.getText(sourceFile);
  if (node.initializer) return node.initializer.getText(sourceFile);
  return "";
}

function addRecord(sourceFile, kind, name, scope, node, signature = "", doc = "") {
  if (!name) return;
  records.push({
    file: path.relative(root, sourceFile.fileName),
    line: lineOf(sourceFile, node),
    kind,
    name,
    scope: scope.join("."),
    signature,
    doc,
    language: sourceFile.fileName.endsWith(".tsx") ? "TSX" : "TypeScript",
  });
}

function addInterface(sourceFile, kind, name, scope, node, signature = "", doc = "") {
  if (!name) return;
  interfaces.push({
    file: path.relative(root, sourceFile.fileName),
    line: lineOf(sourceFile, node),
    kind,
    name,
    scope: scope.join("."),
    signature,
    doc,
    language: sourceFile.fileName.endsWith(".tsx") ? "TSX" : "TypeScript",
  });
}

function visit(sourceFile, node, scope) {
  let childScope = scope;

  if (ts.isClassDeclaration(node) || ts.isClassExpression(node)) {
    const name = nodeName(node.name, sourceFile) || `<anonymous-class@L${lineOf(sourceFile, node)}>`;
    const heritage = node.heritageClauses?.map((item) => item.getText(sourceFile)).join(" ") || "";
    addRecord(sourceFile, "类", name, scope, node, heritage, jsDocText(node));
    childScope = [...scope, name];
  } else if (ts.isInterfaceDeclaration(node)) {
    const name = node.name.text;
    const heritage = node.heritageClauses?.map((item) => item.getText(sourceFile)).join(" ") || "";
    addRecord(sourceFile, "接口", name, scope, node, heritage, jsDocText(node));
    addInterface(sourceFile, "TypeScript 接口", name, scope, node, heritage, jsDocText(node));
    childScope = [...scope, name];
  } else if (ts.isTypeAliasDeclaration(node)) {
    const name = node.name.text;
    const signature = node.type.getText(sourceFile);
    addRecord(sourceFile, "类型别名", name, scope, node, signature, jsDocText(node));
    addInterface(sourceFile, "TypeScript 类型", name, scope, node, signature, jsDocText(node));
    childScope = [...scope, name];
  } else if (ts.isEnumDeclaration(node)) {
    const name = node.name.text;
    addRecord(sourceFile, "枚举", name, scope, node, "", jsDocText(node));
    addInterface(sourceFile, "TypeScript 枚举", name, scope, node, "", jsDocText(node));
    childScope = [...scope, name];
  } else if (
    ts.isFunctionDeclaration(node) ||
    ts.isFunctionExpression(node) ||
    ts.isArrowFunction(node) ||
    ts.isMethodDeclaration(node) ||
    ts.isMethodSignature(node) ||
    ts.isGetAccessorDeclaration(node) ||
    ts.isSetAccessorDeclaration(node) ||
    ts.isConstructorDeclaration(node)
  ) {
    const name = ts.isConstructorDeclaration(node) ? "constructor" : functionName(node, sourceFile);
    if (name) {
      let kind = "函数";
      if (ts.isMethodDeclaration(node) || ts.isMethodSignature(node)) kind = "方法";
      if (ts.isGetAccessorDeclaration(node) || ts.isSetAccessorDeclaration(node)) kind = "访问器";
      if (ts.isConstructorDeclaration(node)) kind = "构造函数";
      addRecord(
        sourceFile,
        kind,
        name,
        scope,
        node,
        functionSignature(node, name, sourceFile),
        jsDocText(node),
      );
      if (ts.isMethodSignature(node)) {
        addInterface(
          sourceFile,
          "TypeScript 接口方法",
          name,
          scope,
          node,
          functionSignature(node, name, sourceFile),
          jsDocText(node),
        );
      }
      childScope = [...scope, name];
    }
  } else if (ts.isVariableDeclaration(node)) {
    for (const name of bindingNames(node.name)) {
      addRecord(
        sourceFile,
        scope.length ? "局部变量" : "模块变量",
        name,
        scope,
        node,
        declarationType(node, sourceFile),
        jsDocText(node.parent?.parent || node),
      );
    }
  } else if (ts.isParameter(node)) {
    for (const name of bindingNames(node.name)) {
      addRecord(sourceFile, "参数", name, scope, node, declarationType(node, sourceFile), jsDocText(node));
    }
  } else if (ts.isPropertyDeclaration(node)) {
    addRecord(
      sourceFile,
      "类字段",
      nodeName(node.name, sourceFile),
      scope,
      node,
      declarationType(node, sourceFile),
      jsDocText(node),
    );
  } else if (ts.isPropertySignature(node)) {
    const name = nodeName(node.name, sourceFile);
    addRecord(sourceFile, "接口字段", name, scope, node, declarationType(node, sourceFile), jsDocText(node));
    addInterface(
      sourceFile,
      "TypeScript 接口字段",
      name,
      scope,
      node,
      declarationType(node, sourceFile),
      jsDocText(node),
    );
  } else if (ts.isEnumMember(node)) {
    addRecord(
      sourceFile,
      "枚举成员",
      nodeName(node.name, sourceFile),
      scope,
      node,
      node.initializer?.getText(sourceFile) || "",
      jsDocText(node),
    );
  } else if (ts.isCatchClause(node) && node.variableDeclaration) {
    for (const name of bindingNames(node.variableDeclaration.name)) {
      addRecord(
        sourceFile,
        "局部变量",
        name,
        scope,
        node.variableDeclaration,
        declarationType(node.variableDeclaration, sourceFile),
        "",
      );
    }
  }

  ts.forEachChild(node, (child) => visit(sourceFile, child, childScope));
}

for (const file of files) {
  try {
    const sourceText = fs.readFileSync(file, "utf8");
    const scriptKind = file.endsWith(".tsx")
      ? ts.ScriptKind.TSX
      : file.endsWith(".jsx")
        ? ts.ScriptKind.JSX
        : file.endsWith(".js")
          ? ts.ScriptKind.JS
          : ts.ScriptKind.TS;
    const sourceFile = ts.createSourceFile(file, sourceText, ts.ScriptTarget.Latest, true, scriptKind);
    visit(sourceFile, sourceFile, []);
    for (const diagnostic of sourceFile.parseDiagnostics) {
      const line = diagnostic.start == null
        ? 1
        : sourceFile.getLineAndCharacterOfPosition(diagnostic.start).line + 1;
      errors.push({
        file: path.relative(root, file),
        line,
        message: ts.flattenDiagnosticMessageText(diagnostic.messageText, " "),
      });
    }
  } catch (error) {
    errors.push({
      file: path.relative(root, file),
      line: 1,
      message: error instanceof Error ? error.message : String(error),
    });
  }
}

process.stdout.write(JSON.stringify({ records, interfaces, errors }));
