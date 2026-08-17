import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig, type DefaultTheme } from 'vitepress'

const docsRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const repo = process.env.GITHUB_REPOSITORY || 'leoriczhang/teamEvolver'
const base = process.env.DOCS_BASE || '/'

const sectionNames: Record<string, string> = {
  'getting-started': 'Getting Started',
  concepts: 'Concepts',
  guides: 'Guides',
  'agent-integrations': 'Agent Integrations',
  api: 'API Reference',
  faq: 'FAQ',
  about: 'About',
  design: 'Design Notes'
}

const zhSectionNames: Record<string, string> = {
  'getting-started': '开始使用',
  concepts: '核心概念',
  guides: '使用指南',
  'agent-integrations': 'Agent 接入',
  api: 'API 参考',
  faq: '常见问题',
  about: '关于',
  design: '设计文档'
}

const navLabels = {
  en: {
    start: 'Getting Started',
    concepts: 'Concepts',
    guide: 'Guides',
    integrate: 'Integrations',
    api: 'API Reference',
    faq: 'FAQ',
    about: 'About'
  },
  zh: {
    start: '开始使用',
    concepts: '核心概念',
    guide: '使用指南',
    integrate: 'Agent 接入',
    api: 'API 参考',
    faq: '常见问题',
    about: '关于'
  }
}

function titleFromMarkdown(filePath: string): string {
  const content = fs.readFileSync(filePath, 'utf8')
  const heading = content.match(/^#\s+(.+)$/m)?.[1]
  const fallback = path.basename(filePath, '.md')
  return (heading || fallback).replace(/^\d+[-_]/, '').trim()
}

function linkFor(filePath: string): string {
  const relativePath = path.relative(docsRoot, filePath).replaceAll(path.sep, '/')
  return `/${relativePath.replace(/\.md$/, '')}`
}

function sidebarSection(dir: string, title: string, collapsed = true): DefaultTheme.SidebarItem {
  const absoluteDir = path.join(docsRoot, dir)
  const items = fs
    .readdirSync(absoluteDir)
    .filter((file) => file.endsWith('.md'))
    .sort((a, b) => a.localeCompare(b, undefined, { numeric: true }))
    .map((file) => {
      const filePath = path.join(absoluteDir, file)
      return {
        text: titleFromMarkdown(filePath),
        link: linkFor(filePath)
      }
    })
  return { text: title, collapsed, items }
}

const gettingStartedItems = {
  en: [
    ['01-introduction.md', 'Introduction'],
    ['02-quickstart.md', 'Quick Start'],
    ['03-installation.md', 'Installation']
  ],
  zh: [
    ['01-introduction.md', '产品简介'],
    ['02-quickstart.md', '快速开始'],
    ['03-installation.md', '安装部署']
  ]
} as const

const conceptsItems = {
  en: {
    overview: 'Architecture Overview',
    groups: [
      {
        text: 'Core Model',
        items: [
          ['02-evolution-loop.md', 'Evolution Loop'],
          ['03-skills.md', 'Skills'],
          ['04-memory.md', 'Memory & DreamCycle'],
          ['05-sessions.md', 'Sessions & Evidence']
        ]
      },
      {
        text: 'Validation & Safety',
        items: [
          ['06-true-replay.md', 'True Replay'],
          ['07-checklist.md', 'Checklist Gates'],
          ['08-publish-rollback.md', 'Publish & Rollback']
        ]
      }
    ]
  },
  zh: {
    overview: '架构总览',
    groups: [
      {
        text: '核心模型',
        items: [
          ['02-evolution-loop.md', '进化闭环'],
          ['03-skills.md', 'Skill 体系'],
          ['04-memory.md', 'Memory 与 DreamCycle'],
          ['05-sessions.md', 'Session 与 Evidence']
        ]
      },
      {
        text: '验证与安全',
        items: [
          ['06-true-replay.md', 'True Replay'],
          ['07-checklist.md', 'Checklist 门禁'],
          ['08-publish-rollback.md', '发布与回滚']
        ]
      }
    ]
  }
} as const

const guidesItems = {
  en: {
    groups: [
      {
        text: 'Setup & Config',
        items: [
          ['01-configuration.md', 'Configuration'],
          ['02-deployment.md', 'Production Deployment'],
          ['03-console.md', 'Web Console']
        ]
      },
      {
        text: 'Operations',
        items: [
          ['04-observability.md', 'Observability (Langfuse)'],
          ['05-daemon-cli.md', 'Daemon & CLI'],
          ['06-troubleshooting.md', 'Troubleshooting']
        ]
      },
      {
        text: 'Advanced',
        items: [
          ['07-skill-miner.md', 'Skill Miner'],
          ['08-prompt-studio.md', 'Prompt Studio']
        ]
      }
    ]
  },
  zh: {
    groups: [
      {
        text: '安装与配置',
        items: [
          ['01-configuration.md', '配置参考'],
          ['02-deployment.md', '生产部署'],
          ['03-console.md', 'Web 控制台']
        ]
      },
      {
        text: '运维与操作',
        items: [
          ['04-observability.md', '可观测性 (Langfuse)'],
          ['05-daemon-cli.md', '守护进程与 CLI'],
          ['06-troubleshooting.md', '故障排查']
        ]
      },
      {
        text: '进阶功能',
        items: [
          ['07-skill-miner.md', 'Skill Miner'],
          ['08-prompt-studio.md', 'Prompt Studio']
        ]
      }
    ]
  }
} as const

const integrationItems = {
  en: {
    overview: 'Integration Overview',
    groups: [
      {
        text: 'Protocol',
        items: [
          ['02-protocol-v1.md', 'Protocol V1 Specification']
        ]
      },
      {
        text: 'Supported Agents',
        items: [
          ['03-hermes.md', 'Hermes Coding Agent'],
          ['04-agentshub-pi.md', 'AgentsHub (Pi)']
        ]
      },
      {
        text: 'Build Your Own',
        items: [
          ['05-custom-agent.md', 'Custom Agent Integration']
        ]
      }
    ]
  },
  zh: {
    overview: '接入概览',
    groups: [
      {
        text: '协议规范',
        items: [
          ['02-protocol-v1.md', 'Protocol V1 协议规范']
        ]
      },
      {
        text: '已支持的 Agent',
        items: [
          ['03-hermes.md', 'Hermes Coding Agent'],
          ['04-agentshub-pi.md', 'AgentsHub (Pi)']
        ]
      },
      {
        text: '自定义接入',
        items: [
          ['05-custom-agent.md', '自定义 Agent 接入']
        ]
      }
    ]
  }
} as const

const apiItems = {
  en: {
    overview: 'Overview',
    groups: [
      {
        text: 'Agent Protocol',
        items: [
          ['02-agent-register.md', 'Agent Registration'],
          ['03-session-ingest.md', 'Session Ingest'],
          ['04-context-workspace.md', 'Context Workspace'],
          ['05-replay-branch.md', 'Replay Branch'],
          ['06-skill-sync.md', 'Skill Sync']
        ]
      },
      {
        text: 'Control Plane',
        items: [
          ['07-health-status.md', 'Health & Status'],
          ['08-sessions-api.md', 'Sessions API'],
          ['09-skills-admin.md', 'Skills Admin'],
          ['10-validation.md', 'Validation & Candidates']
        ]
      },
      {
        text: 'Documentation',
        items: [['99-docs-maintenance.md', 'Docs Maintenance Guide']]
      }
    ]
  },
  zh: {
    overview: '概览',
    groups: [
      {
        text: 'Agent 协议接口',
        items: [
          ['02-agent-register.md', 'Agent 注册'],
          ['03-session-ingest.md', 'Session 上报'],
          ['04-context-workspace.md', 'Context Workspace'],
          ['05-replay-branch.md', 'Replay 分支执行'],
          ['06-skill-sync.md', 'Skill 同步']
        ]
      },
      {
        text: '控制面接口',
        items: [
          ['07-health-status.md', '健康与状态'],
          ['08-sessions-api.md', 'Session 查询'],
          ['09-skills-admin.md', 'Skill 管理'],
          ['10-validation.md', '验证与 Candidate']
        ]
      },
      {
        text: '文档维护',
        items: [['99-docs-maintenance.md', '文档维护指南']]
      }
    ]
  }
} as const

type StructuredSidebarCopy = {
  readonly overview: string
  readonly groups: ReadonlyArray<{
    readonly text: string
    readonly items: ReadonlyArray<readonly [string, string]>
  }>
}

function configuredSidebarItem(
  locale: 'en' | 'zh',
  section: string,
  [file, text]: readonly [string, string]
): DefaultTheme.SidebarItem {
  return {
    text,
    link: linkFor(path.join(docsRoot, locale, section, file))
  }
}

function configuredSidebarGroups(
  locale: 'en' | 'zh',
  section: string,
  groups: StructuredSidebarCopy['groups']
): DefaultTheme.SidebarItem[] {
  return groups.map((group) => ({
    text: group.text,
    collapsed: false,
    items: group.items.map((item) => configuredSidebarItem(locale, section, item))
  }))
}

function structuredSidebarSection(
  locale: 'en' | 'zh',
  section: string,
  title: string,
  copy: StructuredSidebarCopy,
  collapsed = true,
  overviewFile = '01-overview.md'
): DefaultTheme.SidebarItem {
  return {
    text: title,
    collapsed,
    items: [
      configuredSidebarItem(locale, section, [overviewFile, copy.overview]),
      ...configuredSidebarGroups(locale, section, copy.groups)
    ]
  }
}

function getStartedSection(locale: 'en' | 'zh', title: string, collapsed = true): DefaultTheme.SidebarItem {
  return {
    text: title,
    collapsed,
    items: gettingStartedItems[locale].map((item) => configuredSidebarItem(locale, 'getting-started', item))
  }
}

function conceptsSection(locale: 'en' | 'zh', title: string, collapsed = true): DefaultTheme.SidebarItem {
  return structuredSidebarSection(locale, 'concepts', title, conceptsItems[locale], collapsed, '01-architecture.md')
}

function guidesSection(locale: 'en' | 'zh', title: string, collapsed = true): DefaultTheme.SidebarItem {
  return {
    text: title,
    collapsed,
    items: configuredSidebarGroups(locale, 'guides', guidesItems[locale].groups)
  }
}

function integrationsSection(locale: 'en' | 'zh', title: string, collapsed = true): DefaultTheme.SidebarItem {
  return structuredSidebarSection(locale, 'agent-integrations', title, integrationItems[locale], collapsed)
}

function apiSection(locale: 'en' | 'zh', title: string, collapsed = true): DefaultTheme.SidebarItem {
  return structuredSidebarSection(locale, 'api', title, apiItems[locale], collapsed)
}

function aboutSection(locale: 'en' | 'zh', title: string, collapsed = true): DefaultTheme.SidebarItem {
  return sidebarSection(`${locale}/about`, title, collapsed)
}

function faqSection(locale: 'en' | 'zh', title: string, collapsed = true): DefaultTheme.SidebarItem {
  return sidebarSection(`${locale}/faq`, title, collapsed)
}

const enNav: DefaultTheme.NavItem[] = [
  { text: navLabels.en.start, link: '/en/getting-started/01-introduction', activeMatch: '/en/getting-started/' },
  { text: navLabels.en.concepts, link: '/en/concepts/01-architecture', activeMatch: '/en/concepts/' },
  { text: navLabels.en.guide, link: '/en/guides/01-configuration', activeMatch: '/en/guides/' },
  { text: navLabels.en.integrate, link: '/en/agent-integrations/01-overview', activeMatch: '/en/agent-integrations/' },
  { text: navLabels.en.api, link: '/en/api/01-overview', activeMatch: '/en/api/' },
  { text: navLabels.en.faq, link: '/en/faq/faq', activeMatch: '/en/faq/' },
  { text: navLabels.en.about, link: '/en/about/01-about', activeMatch: '/en/about/' }
]

const zhNav: DefaultTheme.NavItem[] = [
  { text: navLabels.zh.start, link: '/zh/getting-started/01-introduction', activeMatch: '/zh/getting-started/' },
  { text: navLabels.zh.concepts, link: '/zh/concepts/01-architecture', activeMatch: '/zh/concepts/' },
  { text: navLabels.zh.guide, link: '/zh/guides/01-configuration', activeMatch: '/zh/guides/' },
  { text: navLabels.zh.integrate, link: '/zh/agent-integrations/01-overview', activeMatch: '/zh/agent-integrations/' },
  { text: navLabels.zh.api, link: '/zh/api/01-overview', activeMatch: '/zh/api/' },
  { text: navLabels.zh.faq, link: '/zh/faq/faq', activeMatch: '/zh/faq/' },
  { text: navLabels.zh.about, link: '/zh/about/01-about', activeMatch: '/zh/about/' }
]

export default defineConfig({
  base,
  title: 'teamEvolver',
  description: 'Agent 团队能力进化控制面 / Agent Team Capability Evolution Control Plane',
  cleanUrls: true,
  lastUpdated: true,
  ignoreDeadLinks: true,
  head: [
    ['link', { rel: 'icon', type: 'image/svg+xml', href: `${base}assets/logo.svg` }]
  ],
  themeConfig: {
    socialLinks: [
      { icon: 'github', link: `https://github.com/${repo}` }
    ],
    footer: {
      message: 'Released under the MIT License.',
      copyright: 'Copyright teamEvolver contributors'
    }
  },
  locales: {
    en: {
      label: 'English',
      lang: 'en-US',
      link: '/en/',
      themeConfig: {
        nav: enNav,
        outline: { level: [2, 3] },
        sidebar: {
          '/en/getting-started/': [
            getStartedSection('en', sectionNames['getting-started'], false)
          ],
          '/en/concepts/': [
            conceptsSection('en', sectionNames.concepts, false)
          ],
          '/en/guides/': [
            guidesSection('en', sectionNames.guides, false)
          ],
          '/en/agent-integrations/': [
            integrationsSection('en', sectionNames['agent-integrations'], false)
          ],
          '/en/api/': [
            apiSection('en', sectionNames.api, false)
          ],
          '/en/faq/': [
            faqSection('en', sectionNames.faq, false)
          ],
          '/en/about/': [
            aboutSection('en', sectionNames.about, false)
          ],
          '/design/': [
            sidebarSection('design', sectionNames.design, false)
          ]
        }
      }
    },
    zh: {
      label: '简体中文',
      lang: 'zh-CN',
      link: '/zh/',
      title: 'teamEvolver',
      description: 'Agent 团队能力进化控制面',
      themeConfig: {
        nav: zhNav,
        sidebar: {
          '/zh/getting-started/': [
            getStartedSection('zh', zhSectionNames['getting-started'], false)
          ],
          '/zh/concepts/': [
            conceptsSection('zh', zhSectionNames.concepts, false)
          ],
          '/zh/guides/': [
            guidesSection('zh', zhSectionNames.guides, false)
          ],
          '/zh/agent-integrations/': [
            integrationsSection('zh', zhSectionNames['agent-integrations'], false)
          ],
          '/zh/api/': [
            apiSection('zh', zhSectionNames.api, false)
          ],
          '/zh/faq/': [
            faqSection('zh', zhSectionNames.faq, false)
          ],
          '/zh/about/': [
            aboutSection('zh', zhSectionNames.about, false)
          ]
        },
        outline: {
          label: '页面导航',
          level: [2, 3]
        },
        docFooter: { prev: '上一页', next: '下一页' },
        darkModeSwitchLabel: '外观',
        sidebarMenuLabel: '菜单',
        returnToTopLabel: '返回顶部',
        langMenuLabel: '切换语言'
      }
    }
  }
})
