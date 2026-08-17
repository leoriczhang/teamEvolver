#!/usr/bin/env node
/**
 * teamEvolver docs reference checker.
 *
 * Scans all markdown files under docs/zh and docs/en for:
 *   1. Code entry references  -> verify the file exists in the repo
 *   2. Internal markdown links -> verify target exists
 *   3. Image references        -> verify image file exists
 *
 * Usage:
 *   npm run check:refs
 *   node scripts/check-docs-refs.mjs [--strict]
 *
 * Exit code 0 = all refs valid; non-zero = broken refs found.
 */

import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const docsRoot = path.resolve(__dirname, '..')
const repoRoot = path.resolve(docsRoot, '..')

const strict = process.argv.includes('--strict')

// ---------------------------------------------------------------------------
// Collect all markdown files
// ---------------------------------------------------------------------------

function walkMarkdown(dir, results = []) {
  for (const entry of fs.readdirSync(dir)) {
    if (entry === 'node_modules' || entry === '.vitepress') continue
    const abs = path.join(dir, entry)
    const stat = fs.statSync(abs)
    if (stat.isDirectory()) {
      walkMarkdown(abs, results)
    } else if (entry.endsWith('.md')) {
      results.push(abs)
    }
  }
  return results
}

const mdFiles = [
  ...walkMarkdown(path.join(docsRoot, 'zh')),
  ...walkMarkdown(path.join(docsRoot, 'en')),
  ...walkMarkdown(path.join(docsRoot, 'design')),
]

// ---------------------------------------------------------------------------
// Regex patterns for reference extraction
// ---------------------------------------------------------------------------

// Matches [text](file:///abs/path) and file:///abs/path in backticks/text
// file:///path means the absolute path is /path (third slash starts the path)
const codeFileRegex = /file:\/\/\/([^\s)\]"'<>`]+)/g

// Matches relative/absolute markdown links but exclude file:/// URLs
const mdLinkRegex = /\[([^\]]*)\]\((?!file:)([^)]+\.md[^)]*)\)/g

// Matches ![alt](/assets/image.png) or ![alt](./assets/image.png)
const imageRegex = /!\[([^\]]*)\]\(([^)]+)\)/g

// Matches "code entry" lines like:
//   teamEvolver/proxy/routes.py:register_agent
//   teamEvolver/evolve/prompt_studio.py
const codeEntryRegex = /(?:^|\s)`(teamEvolver\/[a-zA-Z0-9_/.]+\.py)(?::([a-zA-Z_][a-zA-Z0-9_.]*))?`/gm

// Matches code-entry lines that look like: `- `teamEvolver/...``
const codeEntryListRegex = /`(teamEvolver\/[a-zA-Z0-9_/.]+\.py)`/g

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function fileExists(p) {
  try {
    return fs.existsSync(p) && fs.statSync(p).isFile()
  } catch {
    return false
  }
}

function pathExists(p) {
  try {
    return fs.existsSync(p)
  } catch {
    return false
  }
}

function normalizePath(p) {
  return p.replace(/\\/g, '/').split('?')[0].split('#')[0]
}

function resolveInternalLink(link, fromFile) {
  const clean = normalizePath(link)
  if (clean.startsWith('/')) {
    // Absolute path relative to docsRoot
    return path.join(docsRoot, clean)
  }
  // Relative path
  return path.resolve(path.dirname(fromFile), clean)
}

function resolveImage(src, fromFile) {
  const clean = normalizePath(src)
  if (clean.startsWith('http://') || clean.startsWith('https://')) return null // external
  if (clean.startsWith('/')) {
    return path.join(docsRoot, clean)
  }
  return path.resolve(path.dirname(fromFile), clean)
}

// ---------------------------------------------------------------------------
// Check
// ---------------------------------------------------------------------------

const errors = []
const warnings = []
let filesChecked = 0
let refsChecked = 0

for (const mdFile of mdFiles) {
  filesChecked++
  const rel = path.relative(docsRoot, mdFile)
  const content = fs.readFileSync(mdFile, 'utf8')
  const lines = content.split('\n')

  // 1. Check file:/// references (file:///path -> absolute path /path)
  for (const match of content.matchAll(codeFileRegex)) {
    refsChecked++
    let refPath = '/' + match[1]  // restore leading slash after file://
    // Skip fragment/query
    refPath = refPath.split('#')[0].split('?')[0]
    // Handle directory references (ending with /)
    if (refPath.endsWith('/')) {
      if (!pathExists(refPath)) {
        errors.push(`[${rel}] Missing directory: ${refPath}`)
      }
    } else {
      if (!fileExists(refPath)) {
        errors.push(`[${rel}] Broken file:/// reference: ${refPath}`)
      }
    }
  }

  // 2. Check code entry references (teamEvolver/...py[:Symbol])
  for (const match of content.matchAll(codeEntryRegex)) {
    refsChecked++
    const codeRelPath = match[1]
    const symbol = match[2]
    const absPath = path.join(repoRoot, codeRelPath)
    if (!fileExists(absPath)) {
      errors.push(`[${rel}] Missing code file: ${codeRelPath}`)
      continue
    }
    if (symbol) {
      // Verify symbol exists in file (basic check)
      const fileContent = fs.readFileSync(absPath, 'utf8')
      // Check for def/class/function/async def of symbol
      const symName = symbol.split('.').pop()
      const symbolRegex = new RegExp(
        `(?:def|class|async\\s+def)\\s+${symName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\b`
      )
      if (!symbolRegex.test(fileContent)) {
        warnings.push(`[${rel}] Symbol "${symName}" not found in ${codeRelPath}`)
      }
    }
  }

  // 3. Check internal markdown links
  for (const match of content.matchAll(mdLinkRegex)) {
    refsChecked++
    const linkTarget = match[2]
    if (linkTarget.startsWith('http://') || linkTarget.startsWith('https://')) continue
    if (linkTarget.startsWith('mailto:')) continue
    const targetPath = resolveInternalLink(linkTarget, mdFile)
    // Handle links that map to .md or to directories (index.md)
    const candidates = [
      targetPath,
      targetPath + '.md',
      path.join(targetPath, 'index.md')
    ]
    if (!candidates.some(p => fileExists(p))) {
      errors.push(`[${rel}] Broken internal link: ${linkTarget} -> ${path.relative(docsRoot, targetPath)}`)
    }
  }

  // 4. Check images
  for (const match of content.matchAll(imageRegex)) {
    refsChecked++
    const imgSrc = match[2]
    if (imgSrc.startsWith('http://') || imgSrc.startsWith('https://')) continue
    if (imgSrc.startsWith('data:')) continue
    const imgPath = resolveImage(imgSrc, mdFile)
    if (imgPath && !fileExists(imgPath)) {
      errors.push(`[${rel}] Missing image: ${imgSrc} -> ${path.relative(docsRoot, imgPath)}`)
    }
  }
}

// 5. Check that zh/ and en/ have parallel structure (warning, not error)
function listMdDirs(root) {
  const result = new Set()
  for (const f of walkMarkdown(root)) {
    result.add(path.relative(root, f))
  }
  return result
}

const zhFiles = listMdDirs(path.join(docsRoot, 'zh'))
const enFiles = listMdDirs(path.join(docsRoot, 'en'))

const missingInEn = [...zhFiles].filter(f => !enFiles.has(f))
const missingInZh = [...enFiles].filter(f => !zhFiles.has(f))

for (const f of missingInEn) {
  warnings.push(`[zh/${f}] exists but en/ mirror is missing: en/${f}`)
}
for (const f of missingInZh) {
  warnings.push(`[en/${f}] exists but zh/ mirror is missing: zh/${f}`)
}

// 6. Check that all JSON schema files referenced in docs exist
const schemasDir = path.join(docsRoot, 'schemas')
if (fs.existsSync(schemasDir)) {
  const schemas = fs.readdirSync(schemasDir).filter(f => f.endsWith('.schema.json'))
  // referenced schema names from docs
  const schemaRefs = new Set()
  for (const mdFile of mdFiles) {
    const content = fs.readFileSync(mdFile, 'utf8')
    for (const m of content.matchAll(/([a-z0-9-]+\.schema\.json)/g)) {
      schemaRefs.add(m[1])
    }
  }
  for (const ref of schemaRefs) {
    if (!fileExists(path.join(schemasDir, ref))) {
      errors.push(`Referenced schema file missing: schemas/${ref}`)
    }
  }
}

// ---------------------------------------------------------------------------
// Report
// ---------------------------------------------------------------------------

console.log('='.repeat(60))
console.log('teamEvolver Docs Reference Check')
console.log('='.repeat(60))
console.log(`Files checked: ${filesChecked}`)
console.log(`References checked: ${refsChecked}`)
console.log(`Errors: ${errors.length}`)
console.log(`Warnings: ${warnings.length}`)
console.log()

if (warnings.length > 0) {
  console.log('── Warnings ──')
  for (const w of warnings) console.log(`  ⚠  ${w}`)
  console.log()
}

if (errors.length > 0) {
  console.log('── Errors ──')
  for (const e of errors) console.log(`  ✗  ${e}`)
  console.log()
}

if (errors.length === 0) {
  console.log('✓ All references are valid.')
} else {
  console.log(`✗ Found ${errors.length} broken reference(s).`)
}

process.exit(errors.length > 0 || (strict && warnings.length > 0) ? 1 : 0)
