# ruff: noqa: E501
"""Build a self-contained HTML graph from an OpenViking compiled Wiki.

OpenViking's ``examples/compile/graph-show/llm-wiki`` example establishes the
display contract, but graph rendering is not currently exposed as a server
REST endpoint.  This module keeps that boundary explicit: callers obtain all
Wiki documents through authenticated OpenViking APIs, then pass them here for
an in-memory HTML render.  No Wiki content is persisted by this renderer.
"""

from __future__ import annotations

import html
import json
import posixpath
import re
from typing import Any, Mapping

_FRONTMATTER_RE = re.compile(r"\A---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n|\Z)", re.DOTALL)
_CATEGORY_ALIASES = {
    "index": "index",
    "entity": "entity",
    "entities": "entity",
    "concept": "concept",
    "concepts": "concept",
    "method": "method",
    "methods": "method",
    "comparison": "comparison",
    "comparisons": "comparison",
    "analysis": "analysis",
    "analyses": "analysis",
    "synthesis": "analysis",
    "summary": "summary",
    "summaries": "summary",
    "source": "source",
    "sources": "source",
    "audit": "audit",
}
_CATEGORY_STYLE = {
    "index": ("导航", "#E46A76"),
    "entity": ("实体", "#2A9D8F"),
    "concept": ("概念", "#4F7CAC"),
    "method": ("方法", "#7C6EB0"),
    "comparison": ("比较", "#3BA7B8"),
    "analysis": ("分析", "#D89B52"),
    "summary": ("摘要", "#C86A92"),
    "source": ("来源", "#78909C"),
    "audit": ("构建审计", "#9A6FAF"),
    "other": ("其他", "#9AA7B2"),
}


def _frontmatter(content: str) -> tuple[dict[str, str], str]:
    match = _FRONTMATTER_RE.match(content)
    if match is None:
        return {}, content
    metadata: dict[str, str] = {}
    for raw_line in match.group(1).splitlines():
        # Only scalar keys at YAML's top level describe the page. Compile
        # output commonly contains nested ``sources: - title: ...`` records;
        # treating those indented keys as page metadata makes the last source
        # title overwrite the actual Wiki title.
        if raw_line[:1].isspace():
            continue
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        metadata[key.strip().lower()] = value
    return metadata, content[match.end() :]


def _page_title(metadata: Mapping[str, str], body: str, relative_path: str) -> str:
    title = metadata.get("title", "").strip()
    if title:
        return title
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return posixpath.splitext(posixpath.basename(relative_path))[0]


def _page_category(metadata: Mapping[str, str], relative_path: str) -> str:
    declared = metadata.get("type", "").strip().lower()
    if declared in _CATEGORY_ALIASES:
        return _CATEGORY_ALIASES[declared]
    if posixpath.basename(relative_path).lower() == "index.md":
        return "index"
    first_part = relative_path.strip("/").split("/", 1)[0].lower()
    return _CATEGORY_ALIASES.get(first_part, "other")


def build_wiki_graph(
    documents: Mapping[str, str],
    link_index: Mapping[str, Any],
    *,
    wiki_root: str,
) -> dict[str, list[dict[str, Any]]]:
    """Convert Wiki documents plus the resolved link index into graph data."""
    pages = link_index.get("pages") if isinstance(link_index.get("pages"), dict) else {}
    links: list[dict[str, Any]] = []
    degree = {uri: 0 for uri in documents}
    for source_uri in sorted(documents):
        page = pages.get(source_uri) if isinstance(pages, dict) else None
        outgoing = page.get("links", []) if isinstance(page, dict) else []
        for edge in outgoing:
            if not isinstance(edge, dict):
                continue
            target_uri = str(edge.get("target_uri") or "")
            if target_uri not in documents or target_uri == source_uri:
                continue
            labels = edge.get("labels") if isinstance(edge.get("labels"), list) else []
            label = " / ".join(str(item) for item in labels if str(item).strip()) or "关联"
            links.append(
                {
                    "source": source_uri,
                    "target": target_uri,
                    "label": label,
                    "count": max(1, int(edge.get("count") or 1)),
                }
            )
            degree[source_uri] += 1
            degree[target_uri] += 1

    nodes: list[dict[str, Any]] = []
    for uri, content in sorted(documents.items()):
        relative_path = uri[len(wiki_root) :].lstrip("/")
        metadata, body = _frontmatter(content)
        category = _page_category(metadata, relative_path)
        category_label, color = _CATEGORY_STYLE[category]
        nodes.append(
            {
                "id": uri,
                "uri": uri,
                "path": relative_path,
                "title": _page_title(metadata, body, relative_path),
                "description": metadata.get("description", "").strip(),
                "category": category,
                "category_label": category_label,
                "color": color,
                "degree": degree[uri],
                "body": body.strip(),
            }
        )
    category_order = tuple(_CATEGORY_STYLE)
    nodes.sort(
        key=lambda node: (
            category_order.index(node["category"]),
            str(node["title"]).casefold(),
        )
    )
    links.sort(key=lambda edge: (edge["source"], edge["target"], edge["label"]))
    return {"nodes": nodes, "links": links}


def _script_json(value: Any) -> str:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def render_wiki_graph_html(graph: Mapping[str, Any], *, title: str) -> str:
    """Return a dependency-free, sandbox-friendly interactive HTML graph."""
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    links = graph.get("links") if isinstance(graph.get("links"), list) else []
    categories = []
    for node in nodes:
        category = str(node.get("category") or "other") if isinstance(node, dict) else "other"
        if category not in categories:
            categories.append(category)
    legend = "".join(
        '<span class="legend-item"><i style="background:{color}"></i>{label}</span>'.format(
            color=_CATEGORY_STYLE.get(category, _CATEGORY_STYLE["other"])[1],
            label=html.escape(_CATEGORY_STYLE.get(category, _CATEGORY_STYLE["other"])[0]),
        )
        for category in categories
    )
    replacements = {
        "__TITLE__": html.escape(title),
        "__NODE_COUNT__": str(len(nodes)),
        "__EDGE_COUNT__": str(len(links)),
        "__LEGEND__": legend,
        "__GRAPH_JSON__": _script_json({"nodes": nodes, "links": links}),
    }
    document = _HTML_TEMPLATE
    for marker, value in replacements.items():
        document = document.replace(marker, value)
    return document


_HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
  :root{color-scheme:light;--ink:#19252f;--muted:#71808f;--line:#dfe6ed;--accent:#315f86;--selected-color:#78909c}
  *{box-sizing:border-box}html,body{height:100%;margin:0;overflow:hidden;color:var(--ink);font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;-webkit-font-smoothing:antialiased}
  body{background:#eef2f6}button,input{font:inherit}button{color:inherit;cursor:pointer}
  header{position:absolute;z-index:10;top:14px;left:14px;right:14px;height:58px;display:flex;align-items:center;gap:18px;padding:10px 16px;border:1px solid rgba(214,223,232,.9);border-radius:16px;background:rgba(255,255,255,.86);box-shadow:0 12px 34px rgba(41,55,72,.08);backdrop-filter:blur(18px)}
  .brand{min-width:0;flex:1;padding-left:11px;border-left:3px solid var(--selected-color)}.eyebrow{display:block;font-size:8px;font-weight:700;letter-spacing:.18em;color:#98a5b2;margin-bottom:4px}
  header h1{margin:0;font-size:15px;font-weight:650;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .meta{padding:5px 8px;border-radius:99px;background:#f2f5f8;font-size:10px;color:var(--muted);white-space:nowrap}.legend{display:flex;gap:7px 10px;flex-wrap:wrap;max-width:310px}
  .legend-item{display:flex;align-items:center;gap:4px;font-size:9px;color:#778592}.legend-item i{width:6px;height:6px;border-radius:50%;box-shadow:0 0 0 2px rgba(148,163,184,.1)}
  #layout{height:100%;position:relative}.surface{min-width:0;overflow:hidden}
  #graph-pane{position:absolute;inset:0;background-color:#f7f9fc;background-image:radial-gradient(circle at 16% 18%,rgba(79,124,172,.11),transparent 28%),radial-gradient(circle at 82% 78%,rgba(42,157,143,.08),transparent 30%),radial-gradient(rgba(123,142,160,.2) .7px,transparent .7px);background-size:auto,auto,20px 20px}
  #graph{width:100%;height:100%;display:block;touch-action:none;cursor:grab}#graph:active{cursor:grabbing}
  .edge{stroke:#9eafbf;stroke-opacity:.42;transition:opacity .28s,stroke .28s}.edge.active{stroke:var(--selected-color);stroke-opacity:.7}.edge.dim{opacity:.055}
  .cluster-label{fill:#9aa8b5;font-size:9px;font-weight:700;letter-spacing:.14em;text-anchor:middle;pointer-events:none}
  .node{cursor:pointer;outline:none;transition:opacity .3s}.node:focus,.node:focus-visible{outline:none}.node.dim{opacity:.1}.node.dim:hover{opacity:.72}
  .node .halo{opacity:0}.node:hover .halo{opacity:.14}
  .node .core{stroke:rgba(255,255,255,.96);stroke-width:2.5;filter:drop-shadow(0 4px 5px rgba(32,49,67,.2));transition:r .2s,filter .2s}
  .node:hover .core{filter:drop-shadow(0 5px 8px var(--node-color))}.node.selected .core{stroke:#fff;stroke-width:3;filter:drop-shadow(0 5px 9px var(--node-color))}
  .node:focus-visible .core{stroke:#fff;stroke-width:3;filter:drop-shadow(0 5px 9px var(--node-color))}
  .node.selected .halo{transform-box:fill-box;transform-origin:center;animation:halo-breathe 3s ease-in-out infinite}
  .node.selected .visual{animation:water-float 5.8s ease-in-out infinite}
  .node:active .visual{animation-play-state:paused}
  .node text{font-size:11px;font-weight:600;fill:#4f6172;paint-order:stroke;stroke:rgba(249,251,253,.97);stroke-width:4px;stroke-linejoin:round;pointer-events:none}
  .node.selected text{fill:var(--node-color);font-weight:700}
  .toolbar{position:absolute;left:20px;right:20px;top:86px;z-index:4;display:flex;gap:12px;pointer-events:none}
  .searchbox,.zoom-tools{pointer-events:auto;background:rgba(255,255,255,.9);border:1px solid rgba(218,226,234,.96);box-shadow:0 8px 26px rgba(39,54,70,.08);border-radius:11px;backdrop-filter:blur(14px)}
  .searchbox{display:flex;align-items:center;width:min(300px,62%);padding:0 12px;height:38px;color:#91a0ae}
  .searchbox input{min-width:0;flex:1;width:100%;border:0;outline:0;background:transparent;padding:8px;font-size:12px;color:var(--ink)}
  .searchbox:focus-within{border-color:#99b6cf;box-shadow:0 0 0 3px rgba(79,124,172,.1),0 8px 26px rgba(39,54,70,.08)}.search-status{font-size:9px;white-space:nowrap;color:#9aa7b3}
  .zoom-tools{margin-left:auto;display:flex;overflow:hidden}.zoom-tools button{border:0;border-right:1px solid #e7ecf1;background:transparent;padding:0 11px;height:36px;font-size:12px;color:var(--accent)}.zoom-tools button:last-child{border-right:0}
  .zoom-tools button:hover{background:#f1f5f9}.hint{position:absolute;left:22px;bottom:18px;padding:6px 9px;border-radius:8px;background:rgba(255,255,255,.65);font-size:9px;color:#8c9aa8;pointer-events:none}
  .tooltip{position:absolute;z-index:12;max-width:260px;padding:9px 12px;background:#263746;color:#fff;border-radius:9px;font-size:11px;pointer-events:none;opacity:0;transform:translate(10px,12px);box-shadow:0 8px 24px rgba(25,42,55,.22);transition:opacity .12s}.tooltip.show{opacity:.96}
  #detail{position:absolute;z-index:7;top:84px;right:14px;bottom:14px;width:340px;padding:25px 22px;border:1px solid rgba(218,226,234,.95);border-radius:18px;background:rgba(255,255,255,.94);box-shadow:0 18px 50px rgba(38,53,68,.11);overflow:auto;scrollbar-gutter:stable;backdrop-filter:blur(18px)}
  .detail-top{display:flex;align-items:center;gap:8px}.kind{display:flex;align-items:center;gap:6px;font-size:10px;font-weight:700;color:var(--selected-color)}.kind i{width:7px;height:7px;border-radius:50%;box-shadow:0 0 0 3px color-mix(in srgb,var(--selected-color) 12%,transparent)}.degree{margin-left:auto;font-size:9px;color:#96a3ae}
  #detail h2{font-size:21px;font-weight:600;line-height:1.5;margin:16px 0 9px;letter-spacing:-.02em}
  .uri{display:block;font:10px/1.7 ui-monospace,monospace;color:#a0afaa;overflow-wrap:anywhere}
  .description{font-size:12px;line-height:1.8;color:#607181;margin:17px 0;padding:12px 13px;border-radius:10px;background:#f5f8fa}
  .relations{border-top:1px solid #edf1f4;margin-top:20px;padding-top:16px}.relations h3{font-size:10px;font-weight:700;letter-spacing:.06em;color:#95a2ae;margin:0 0 9px}
  .relation{display:flex;gap:9px;align-items:center;text-align:left;width:100%;border:0;border-radius:8px;background:transparent;padding:8px 7px;font-size:11px;line-height:1.55;color:#526575;transition:background .18s,transform .18s}
  .relation:hover{background:#eef4f8;transform:translateX(2px)}.arrow{color:var(--selected-color);font-weight:700}
  .body{white-space:pre-wrap;overflow-wrap:anywhere;font:10.5px/1.85 ui-monospace,SFMono-Regular,Menlo,monospace;color:#687987;border-top:1px solid #edf1f4;margin-top:20px;padding-top:18px}
  #empty{margin-top:45%;font-size:12px;color:var(--muted);text-align:center;line-height:2}
  @keyframes halo-breathe{0%,100%{transform:scale(.95);opacity:.1}50%{transform:scale(1.3);opacity:.24}}
  @keyframes water-float{0%,100%{transform:translate(0,0) rotate(-1deg)}25%{transform:translate(2px,-3px) rotate(.7deg)}50%{transform:translate(0,-5px) rotate(1deg)}75%{transform:translate(-2px,-2px) rotate(-.6deg)}}
  @media(max-width:720px){header{top:8px;left:8px;right:8px;height:54px;padding:8px 12px}.legend{display:none}header h1{font-size:14px}.toolbar{top:72px;left:12px;right:12px}.meta{background:transparent;padding:0}#detail{top:auto;left:8px;right:8px;bottom:8px;width:auto;height:38%;padding:18px;border-radius:15px}.hint{display:none}.zoom-tools #zoom-in,.zoom-tools #zoom-out{display:none}}
  @media(prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important}}
</style>
</head>
<body>
<header><div class="brand"><span class="eyebrow">OPENVIKING · HTML KNOWLEDGE GRAPH</span><h1>__TITLE__</h1></div><div class="meta">__NODE_COUNT__ 个页面 · __EDGE_COUNT__ 条关系</div><div class="legend">__LEGEND__</div></header>
<main id="layout"><section id="graph-pane" class="surface"><div class="toolbar"><label class="searchbox"><span>⌕</span><input id="search" type="search" placeholder="搜索标题或路径" aria-label="搜索知识页面"><small id="search-status" class="search-status" aria-live="polite">__NODE_COUNT__ 个页面</small></label><div class="zoom-tools"><button id="zoom-out" type="button" aria-label="缩小">−</button><button id="zoom-in" type="button" aria-label="放大">＋</button><button id="reset" type="button">适应画布</button></div></div><svg id="graph" role="img" aria-label="知识库关系图"><defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#94a3b8"/></marker></defs><g id="viewport"><g id="clusters"></g><g id="edges"></g><g id="nodes"></g></g></svg><div class="hint">滚轮缩放 · 拖动画布 · 点击节点探索关联</div><div id="tooltip" class="tooltip"></div></section><article id="detail" class="surface"><div id="empty">点击节点查看知识页面<br>搜索后按 Enter 可快速定位</div></article></main>
<script type="application/json" id="graph-data">__GRAPH_JSON__</script>
<script>
(() => {
  const DATA=JSON.parse(document.getElementById('graph-data').textContent);
  const svg=document.getElementById('graph'),viewport=document.getElementById('viewport'),pane=document.getElementById('graph-pane');
  const clusterLayer=document.getElementById('clusters'),edgeLayer=document.getElementById('edges'),nodeLayer=document.getElementById('nodes'),tooltip=document.getElementById('tooltip');
  const byId=new Map(DATA.nodes.map(n=>[n.id,n])),outgoing=new Map(DATA.nodes.map(n=>[n.id,[]])),incoming=new Map(DATA.nodes.map(n=>[n.id,[]]));
  DATA.links.forEach(e=>{outgoing.get(e.source)?.push(e);incoming.get(e.target)?.push(e)});
  const start=DATA.nodes.find(node=>node.category==='index')||DATA.nodes.slice().sort((a,b)=>b.degree-a.degree)[0];
  const positions=new Map(),isolated=DATA.nodes.filter(node=>node.degree===0),connected=DATA.nodes.filter(node=>node.degree>0),categoryTargets=new Map();
  const isolatedCategories=[...new Set(isolated.map(node=>node.category))];
  isolatedCategories.forEach((category,index)=>{const angle=-Math.PI/2+(Math.PI*2*index)/Math.max(isolatedCategories.length,1);categoryTargets.set(category,{x:Math.cos(angle)*390,y:Math.sin(angle)*280})});
  connected.forEach((node,index)=>{const angle=(Math.PI*2*index)/Math.max(connected.length,1);const ring=65+34*Math.sqrt(index);positions.set(node.id,{x:Math.cos(angle)*ring,y:Math.sin(angle)*ring,vx:0,vy:0})});
  isolated.forEach((node,index)=>{const target=categoryTargets.get(node.category),siblings=isolated.filter(item=>item.category===node.category),local=siblings.indexOf(node),angle=local*2.399963;const radius=28*Math.sqrt(local);positions.set(node.id,{x:target.x+Math.cos(angle)*radius,y:target.y+Math.sin(angle)*radius,vx:0,vy:0})});
  function settleLayout(){const nodes=DATA.nodes,links=DATA.links;for(let tick=0;tick<280;tick+=1){const alpha=1-tick/300;for(let i=0;i<nodes.length;i+=1){const a=positions.get(nodes[i].id);for(let j=i+1;j<nodes.length;j+=1){const b=positions.get(nodes[j].id),dx=a.x-b.x,dy=a.y-b.y,dist2=dx*dx+dy*dy+80,dist=Math.sqrt(dist2),force=240*alpha/dist2,collision=Math.max(0,48-dist)*.055*alpha,push=force+collision;a.vx+=dx/dist*push;a.vy+=dy/dist*push;b.vx-=dx/dist*push;b.vy-=dy/dist*push}}for(const edge of links){const a=positions.get(edge.source),b=positions.get(edge.target),dx=b.x-a.x,dy=b.y-a.y,dist=Math.max(1,Math.hypot(dx,dy)),pull=(dist-145)*.018*alpha;a.vx+=dx/dist*pull;a.vy+=dy/dist*pull;b.vx-=dx/dist*pull;b.vy-=dy/dist*pull}for(const node of nodes){const p=positions.get(node.id),target=node.degree>0?{x:0,y:0}:categoryTargets.get(node.category),gravity=node.category==='index'?.024:.012;p.vx+=(target.x-p.x)*gravity*alpha;p.vy+=(target.y-p.y)*gravity*alpha;p.vx*=.78;p.vy*=.78;p.x+=p.vx;p.y+=p.vy}}}
  settleLayout();
  let transform={x:svg.clientWidth/2,y:svg.clientHeight/2,k:1},selected='',focusMode=false,pan=null,drag=null,relaxFrame=0,settleFrames=0;
  const ns='http://www.w3.org/2000/svg',nodeElements=new Map(),edgeElements=[];
  function el(name,attrs={}){const item=document.createElementNS(ns,name);Object.entries(attrs).forEach(([key,value])=>item.setAttribute(key,String(value)));return item}
  function radius(node){return Math.min(18,8+Math.sqrt(Math.max(node.degree,1))*2)}
  isolatedCategories.forEach(category=>{const target=categoryTargets.get(category),items=isolated.filter(node=>node.category===category),label=el('text',{x:target.x,y:target.y-58,class:'cluster-label'});label.textContent=`${items[0]?.category_label||category} · ${items.length}`;clusterLayer.appendChild(label)});
  DATA.links.forEach(edge=>{const a=positions.get(edge.source),b=positions.get(edge.target),line=el('line',{x1:a.x,y1:a.y,x2:b.x,y2:b.y,class:'edge','data-source':edge.source,'data-target':edge.target,'stroke-width':Math.min(3,1+Math.log2(edge.count||1)),'marker-end':'url(#arrow)'});const tip=el('title');tip.textContent=edge.label;line.appendChild(tip);edgeLayer.appendChild(line);edgeElements.push({edge,line})});
  DATA.nodes.forEach(node=>{const p=positions.get(node.id),r=radius(node),group=el('g',{class:'node',transform:`translate(${p.x} ${p.y})`,role:'button','aria-label':node.title,'data-id':node.id});group.appendChild(el('circle',{class:'halo',r:r+10,fill:node.color}));group.appendChild(el('circle',{class:'core',r,fill:node.color}));const text=el('text',{x:r+7,y:4});text.textContent=Array.from(node.title).length>13?Array.from(node.title).slice(0,12).join('')+'…':node.title;group.appendChild(text);group.addEventListener('click',event=>{event.stopPropagation();select(node.id,true)});group.addEventListener('pointerenter',event=>showTooltip(event,node));group.addEventListener('pointermove',event=>showTooltip(event,node));group.addEventListener('pointerleave',hideTooltip);group.addEventListener('pointerdown',event=>{event.preventDefault();event.stopPropagation();drag={id:node.id,pointer:event.pointerId};group.setPointerCapture(event.pointerId);requestRelax(1)});nodeLayer.appendChild(group);nodeElements.set(node.id,group)});
  nodeElements.forEach((group,id)=>{group.style.setProperty('--node-color',byId.get(id).color);const visual=el('g',{class:'visual'});while(group.firstChild)visual.appendChild(group.firstChild);group.appendChild(visual);const label=visual.querySelector('text'),point=positions.get(id);if(point.x< -30){label.setAttribute('x',-radius(byId.get(id))-8);label.setAttribute('text-anchor','end')}});
  function apply(){viewport.setAttribute('transform',`translate(${transform.x} ${transform.y}) scale(${transform.k})`)}
  function relatedIds(id){const ids=new Set([id]);(outgoing.get(id)||[]).forEach(e=>ids.add(e.target));(incoming.get(id)||[]).forEach(e=>ids.add(e.source));return ids}
  function refreshFocus(){document.documentElement.style.setProperty('--selected-color',byId.get(selected)?.color||'#7c8f9d');const related=selected?relatedIds(selected):new Set();nodeElements.forEach((item,id)=>{item.classList.toggle('selected',id===selected);item.classList.toggle('dim',focusMode&&!related.has(id))});edgeElements.forEach(({edge,line})=>{const active=edge.source===selected||edge.target===selected;line.classList.toggle('active',active);line.classList.toggle('dim',focusMode&&!active)})}
  function relationButton(edge,direction){const target=direction==='out'?edge.target:edge.source,node=byId.get(target),button=document.createElement('button');button.className='relation';const arrow=document.createElement('span');arrow.className='arrow';arrow.textContent=direction==='out'?'→':'←';const label=document.createElement('span');label.textContent=edge.label===node?.title?node.title:`${edge.label} · ${node?.title||target}`;button.append(arrow,label);button.addEventListener('click',()=>select(target,true));return button}
  function renderBody(markdown){const body=document.createElement('pre');body.className='body';body.textContent=markdown||'（页面没有正文）';return body}
  function select(id,focus=true){const node=byId.get(id);if(!node)return;selected=id;focusMode=focus;refreshFocus();const detail=document.getElementById('detail');detail.replaceChildren();const top=document.createElement('div');top.className='detail-top';const kind=document.createElement('span');kind.className='kind';const dot=document.createElement('i');dot.style.background=node.color;kind.append(dot,document.createTextNode(node.category_label));const degree=document.createElement('span');degree.className='degree';degree.textContent=`${node.degree} 条直接关系`;top.append(kind,degree);const heading=document.createElement('h2');heading.textContent=node.title;const uri=document.createElement('span');uri.className='uri';uri.textContent=node.path;detail.append(top,heading,uri);if(node.description){const desc=document.createElement('p');desc.className='description';desc.textContent=node.description;detail.appendChild(desc)}const relations=document.createElement('div');relations.className='relations';const total=(outgoing.get(id)||[]).length+(incoming.get(id)||[]).length;if(total){const relTitle=document.createElement('h3');relTitle.textContent=`关联页面 · ${total}`;relations.appendChild(relTitle);(outgoing.get(id)||[]).forEach(e=>relations.appendChild(relationButton(e,'out')));(incoming.get(id)||[]).forEach(e=>relations.appendChild(relationButton(e,'in')));detail.appendChild(relations)}detail.appendChild(renderBody(node.body));detail.scrollTop=0}
  function showTooltip(event,node){const rect=pane.getBoundingClientRect();tooltip.textContent=`${node.title} · ${node.degree} 条关系`;tooltip.style.left=`${event.clientX-rect.left}px`;tooltip.style.top=`${event.clientY-rect.top}px`;tooltip.classList.add('show')}
  function hideTooltip(){tooltip.classList.remove('show')}
  function updateScene(){nodeElements.forEach((group,id)=>{const point=positions.get(id),label=group.querySelector('text'),node=byId.get(id),left=point.x<0;group.setAttribute('transform',`translate(${point.x} ${point.y})`);label.setAttribute('x',left?-radius(node)-8:radius(node)+7);label.setAttribute('text-anchor',left?'end':'start')});edgeElements.forEach(({edge,line})=>{const a=positions.get(edge.source),b=positions.get(edge.target);line.setAttribute('x1',a.x);line.setAttribute('y1',a.y);line.setAttribute('x2',b.x);line.setAttribute('y2',b.y)})}
  function movePoint(id,dx,dy,pinned){if(id===pinned)return;const point=positions.get(id);point.x+=dx;point.y+=dy}
  function relaxStep(pinned,strength){for(let i=0;i<DATA.nodes.length;i+=1){const left=DATA.nodes[i],a=positions.get(left.id);for(let j=i+1;j<DATA.nodes.length;j+=1){const right=DATA.nodes[j],b=positions.get(right.id);let dx=b.x-a.x,dy=b.y-a.y,dist=Math.hypot(dx,dy);if(dist<.01){dx=(j%2?1:-1)*.1;dy=.1;dist=Math.hypot(dx,dy)}const minimum=Math.max(58,radius(left)+radius(right)+28);if(dist>=minimum)continue;const push=Math.min(7,(minimum-dist)*.22*strength),ux=dx/dist,uy=dy/dist;if(left.id===pinned){movePoint(right.id,ux*push*1.7,uy*push*1.7,pinned)}else if(right.id===pinned){movePoint(left.id,-ux*push*1.7,-uy*push*1.7,pinned)}else{movePoint(left.id,-ux*push*.5,-uy*push*.5,pinned);movePoint(right.id,ux*push*.5,uy*push*.5,pinned)}}}for(const edge of DATA.links){const a=positions.get(edge.source),b=positions.get(edge.target),dx=b.x-a.x,dy=b.y-a.y,dist=Math.max(1,Math.hypot(dx,dy)),pull=Math.max(-4,Math.min(4,(dist-145)*.018*strength)),ux=dx/dist,uy=dy/dist;if(edge.source===pinned){movePoint(edge.target,-ux*pull*1.5,-uy*pull*1.5,pinned)}else if(edge.target===pinned){movePoint(edge.source,ux*pull*1.5,uy*pull*1.5,pinned)}else{movePoint(edge.source,ux*pull*.5,uy*pull*.5,pinned);movePoint(edge.target,-ux*pull*.5,-uy*pull*.5,pinned)}}}
  function requestRelax(frames=30){settleFrames=Math.max(settleFrames,frames);if(relaxFrame)return;const animate=()=>{const active=Boolean(drag);if(!active&&settleFrames<=0){relaxFrame=0;return}relaxStep(drag?.id||'',active ? .9 : Math.max(.16,settleFrames/36));updateScene();if(!active)settleFrames-=1;relaxFrame=requestAnimationFrame(animate)};relaxFrame=requestAnimationFrame(animate)}
  function fit(){if(!DATA.nodes.length)return;const values=[...positions.values()],xs=values.map(p=>p.x),ys=values.map(p=>p.y),minX=Math.min(...xs),maxX=Math.max(...xs),minY=Math.min(...ys),maxY=Math.max(...ys),width=Math.max(260,maxX-minX+220),height=Math.max(260,maxY-minY+180),drawer=window.innerWidth>720?370:0,availableWidth=Math.max(260,svg.clientWidth-drawer),availableHeight=Math.max(260,svg.clientHeight-(window.innerWidth<=720?svg.clientHeight*.38:0));transform.k=Math.min(1.15,Math.max(.24,Math.min(availableWidth/width,availableHeight/height)));transform.x=availableWidth/2-(minX+maxX)/2*transform.k;transform.y=82+availableHeight/2-(minY+maxY)/2*transform.k;apply()}
  function zoom(factor){const x=svg.clientWidth/2,y=svg.clientHeight/2,old=transform.k,next=Math.min(3.2,Math.max(.2,old*factor));transform.x=x-(x-transform.x)*next/old;transform.y=y-(y-transform.y)*next/old;transform.k=next;apply()}
  svg.addEventListener('wheel',event=>{event.preventDefault();const rect=svg.getBoundingClientRect(),x=event.clientX-rect.left,y=event.clientY-rect.top,old=transform.k,next=Math.min(3.2,Math.max(.2,old*Math.exp(-event.deltaY*.001)));transform.x=x-(x-transform.x)*next/old;transform.y=y-(y-transform.y)*next/old;transform.k=next;apply()},{passive:false});
  svg.addEventListener('pointerdown',event=>{if(event.target.closest?.('.node'))return;pan={x:event.clientX,y:event.clientY,tx:transform.x,ty:transform.y,moved:false};svg.setPointerCapture(event.pointerId)});svg.addEventListener('pointermove',event=>{if(drag){const rect=svg.getBoundingClientRect(),point=positions.get(drag.id);point.x=(event.clientX-rect.left-transform.x)/transform.k;point.y=(event.clientY-rect.top-transform.y)/transform.k;point.vx=0;point.vy=0;hideTooltip();return}if(!pan)return;pan.moved=true;transform.x=pan.tx+event.clientX-pan.x;transform.y=pan.ty+event.clientY-pan.y;apply()});const endPointer=()=>{if(drag){drag=null;requestRelax(36)}pan=null};svg.addEventListener('pointerup',endPointer);svg.addEventListener('pointercancel',endPointer);
  document.getElementById('zoom-in').addEventListener('click',()=>zoom(1.25));document.getElementById('zoom-out').addEventListener('click',()=>zoom(.8));document.getElementById('reset').addEventListener('click',()=>{focusMode=true;refreshFocus();fit()});
  const search=document.getElementById('search'),searchStatus=document.getElementById('search-status');search.addEventListener('input',event=>{focusMode=false;const query=event.target.value.trim().toLocaleLowerCase(),matches=DATA.nodes.filter(node=>!query||node.title.toLocaleLowerCase().includes(query)||node.path.toLocaleLowerCase().includes(query));const matchIds=new Set(matches.map(node=>node.id));nodeElements.forEach((item,id)=>item.classList.toggle('dim',Boolean(query)&&!matchIds.has(id)));edgeElements.forEach(({line})=>line.classList.toggle('dim',Boolean(query)));searchStatus.textContent=query?`${matches.length} 个匹配`:`${DATA.nodes.length} 个页面`});search.addEventListener('keydown',event=>{if(event.key!=='Enter')return;const query=event.target.value.trim().toLocaleLowerCase(),match=DATA.nodes.find(node=>node.title.toLocaleLowerCase().includes(query)||node.path.toLocaleLowerCase().includes(query));if(match)select(match.id,true)});
  search.addEventListener('input',()=>{if(!search.value.trim()){focusMode=true;refreshFocus()}});
  window.addEventListener('resize',fit);fit();if(start)select(start.id,true);
})();
</script>
</body>
</html>"""
