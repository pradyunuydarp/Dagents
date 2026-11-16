// Builds docs/presentation/survey/dagents-survey-and-design.pptx.
//
// The deck presents the survey paper in docs/reports/dagents_survey_paper/.
// Every number on a slide comes from the paper, the repository's code, or the
// healthcare demo's committed run; change the paper first, then this file.
//
// Run from a folder where pptxgenjs, react-icons, react, react-dom and sharp are
// installed (NODE_PATH may point at its node_modules):
//   node docs/presentation/survey/build_deck.js docs/presentation/survey/dagents-survey-and-design.pptx
// then apply the theme colours with the pptx skill's apply_theme.js.

const path = require("path");
const pptxgen = require("pptxgenjs");
const React = require("react");
const ReactDOMServer = require("react-dom/server");
const sharp = require("sharp");
const Lu = require("react-icons/lu");

const OUT = process.argv[2] || "dagents-survey-and-design.pptx";

// Colour carries meaning only (design/dagents-design-system.css): the three
// decision states, and links. Everything else is ink, grey and white.
const THEME = {
  name: "Dagents survey",
  headFontFace: "Cambria",
  bodyFontFace: "Calibri",
  colors: {
    dk1: "1F2421", // ink
    lt1: "FFFFFF",
    dk2: "151A17", // dark slides
    lt2: "F7F6F2", // content slides
    accent1: "1A7F37", // permit
    accent2: "9A6700", // narrow
    accent3: "CF222E", // deny
    accent4: "5C6670", // secondary text and rules
    accent5: "0969DA", // links
    accent6: "D5D9DD", // hairlines and card borders
    hlink: "0969DA",
    folHlink: "8250DF",
  },
};
const HEX = {
  ink: "1F2421", dark: "151A17", paper: "F7F6F2", white: "FFFFFF",
  permit: "1A7F37", narrow: "9A6700", deny: "CF222E", grey: "5C6670",
  hair: "D5D9DD", soft: "ECEBE6", link: "0969DA", onDark: "C9CEC9",
};

const pres = new pptxgen();
pres.layout = "LAYOUT_WIDE"; // 13.333 x 7.5 in
pres.theme = { headFontFace: THEME.headFontFace, bodyFontFace: THEME.bodyFontFace };
pres.title = "Where governance decisions live";
pres.subject = "A survey of governed analytics and learning across data sources, and the design of Dagents";
pres.author = "Pradyun Devarakonda";
const C = pres.SchemeColor;

const W = 13.333;
const M = 0.6; // side margin
const FOOT = "Where governance decisions live · Dagents";

// ---------------------------------------------------------------- layouts
pres.defineSlideMaster({
  title: "TITLE_DARK",
  background: { color: HEX.dark },
  objects: [
    { placeholder: { options: { name: "title", type: "title", x: M, y: 2.0, w: 10.5, h: 1.6, fontFace: "Cambria", fontSize: 44, bold: false, color: C.background1, valign: "bottom", align: "left", margin: 0 }, text: "" } },
    { placeholder: { options: { name: "body", type: "body", x: M, y: 3.8, w: 10.0, h: 1.2, fontFace: "Calibri", fontSize: 20, color: C.background1, valign: "top", align: "left", margin: 0 }, text: "" } },
    { placeholder: { options: { name: "byline", type: "body", x: M, y: 6.0, w: 9.0, h: 0.8, fontFace: "Calibri", fontSize: 14, color: C.background1, valign: "top", align: "left", margin: 0 }, text: "" } },
  ],
});
pres.defineSlideMaster({
  title: "SECTION_DARK",
  background: { color: HEX.dark },
  objects: [
    { placeholder: { options: { name: "kicker", type: "body", x: M, y: 1.9, w: 6, h: 0.4, fontFace: "Calibri", fontSize: 14, bold: true, color: C.background1, align: "left", margin: 0 }, text: "" } },
    { placeholder: { options: { name: "title", type: "title", x: M, y: 2.35, w: 11.5, h: 1.3, fontFace: "Cambria", fontSize: 40, color: C.background1, valign: "top", align: "left", margin: 0 }, text: "" } },
    { placeholder: { options: { name: "body", type: "body", x: M, y: 3.9, w: 10.5, h: 2.2, fontFace: "Calibri", fontSize: 18, color: C.background1, valign: "top", align: "left", margin: 0 }, text: "" } },
  ],
  slideNumber: { x: W - M - 0.8, y: 7.0, w: 0.8, h: 0.3, fontFace: "Calibri", fontSize: 10, color: HEX.onDark, align: "right" },
});
pres.defineSlideMaster({
  title: "CONTENT",
  background: { color: HEX.paper },
  margin: [0.5, M, 0.6, M],
  objects: [
    { text: { text: FOOT, options: { x: M, y: 7.02, w: 8, h: 0.3, fontFace: "Calibri", fontSize: 10, color: HEX.grey, margin: 0 } } },
    { placeholder: { options: { name: "title", type: "title", x: M, y: 0.85, w: W - 2 * M, h: 0.8, fontFace: "Cambria", fontSize: 30, color: C.text1, valign: "top", align: "left", margin: 0 }, text: "" } },
  ],
  slideNumber: { x: W - M - 0.8, y: 7.02, w: 0.8, h: 0.3, fontFace: "Calibri", fontSize: 10, color: HEX.grey, align: "right" },
});

// ---------------------------------------------------------------- helpers
const iconCache = {};
async function icon(name, color, size = 256) {
  const key = `${name}:${color}`;
  if (iconCache[key]) return iconCache[key];
  const Comp = Lu[name];
  if (!Comp) throw new Error(`no icon ${name}`);
  const svg = ReactDOMServer.renderToStaticMarkup(React.createElement(Comp, { color: `#${color}`, size: String(size) }));
  const png = await sharp(Buffer.from(svg)).png().toBuffer();
  iconCache[key] = "image/png;base64," + png.toString("base64");
  return iconCache[key];
}

function chip(slide, text, opts = {}) {
  // The small label above a title: the deck's one repeated motif.
  const dark = opts.dark;
  slide.addText(text.toUpperCase(), {
    x: opts.x ?? M, y: opts.y ?? 0.42, w: opts.w ?? Math.max(1.2, 0.11 * text.length + 0.4), h: 0.3,
    fontFace: "Calibri", fontSize: 10, bold: true, charSpacing: 1,
    color: dark ? HEX.dark : HEX.white, fill: { color: dark ? HEX.white : HEX.ink },
    align: "center", valign: "middle", margin: 0, isTextBox: true, objectName: "label",
  });
}

function content(section, label, title) {
  const s = pres.addSlide({ masterName: "CONTENT", sectionTitle: section });
  chip(s, label);
  s.addText(title, { placeholder: "title" });
  return s;
}

function card(slide, x, y, w, h, opts = {}) {
  slide.addShape(pres.shapes.RECTANGLE, {
    x, y, w, h, fill: { color: opts.fill || HEX.white }, line: { color: opts.line || HEX.hair, width: 0.75 },
    objectName: opts.name || "card",
  });
}

function text(slide, str, x, y, w, h, opts = {}) {
  slide.addText(str, { x, y, w, h, fontFace: "Calibri", fontSize: 14, color: HEX.ink, valign: "top", margin: 0, isTextBox: true, ...opts });
}

function bullets(slide, items, x, y, w, h, opts = {}) {
  const runs = items.map((it, i) => {
    const t = typeof it === "string" ? [{ text: it }] : it;
    return t.map((r, j) => ({
      text: r.text,
      options: { bullet: j === 0 ? { indent: 14 } : undefined, breakLine: j === t.length - 1 && i < items.length - 1, bold: r.bold, color: r.color, paraSpaceAfter: 6 },
    }));
  }).flat();
  slide.addText(runs, { x, y, w, h, fontFace: "Calibri", fontSize: opts.fontSize || 15, color: HEX.ink, valign: "top", margin: 0, isTextBox: true });
}

const DECISION = { permit: HEX.permit, narrow: HEX.narrow, deny: HEX.deny };
function pill(slide, label, kind, x, y, w = 1.0, h = 0.32) {
  slide.addText(label, {
    x, y, w, h, fontFace: "Calibri", fontSize: 11, bold: true, color: HEX.white,
    fill: { color: DECISION[kind] }, align: "center", valign: "middle", margin: 0, isTextBox: true,
  });
}

async function iconRow(slide, iconName, head, body, x, y, w, opts = {}) {
  const d = 0.62;
  slide.addShape(pres.shapes.OVAL, { x, y, w: d, h: d, fill: { color: opts.dark ? HEX.white : HEX.ink }, line: { color: opts.dark ? HEX.white : HEX.ink } });
  slide.addImage({ data: await icon(iconName, opts.dark ? HEX.ink : HEX.white), x: x + 0.14, y: y + 0.14, w: d - 0.28, h: d - 0.28 });
  text(slide, head, x + d + 0.25, y - 0.02, w - d - 0.25, 0.4, { fontSize: 17, bold: true, color: opts.dark ? HEX.white : HEX.ink });
  if (body) text(slide, body, x + d + 0.25, y + 0.36, w - d - 0.25, opts.bodyH || 0.6, { fontSize: 14, color: opts.dark ? HEX.onDark : HEX.grey });
}

function table(slide, header, rows, x, y, w, colW, opts = {}) {
  const fs = opts.fontSize || 12;
  const head = header.map((h) => ({ text: h, options: { bold: true, color: HEX.ink, fill: { color: HEX.soft }, fontSize: fs } }));
  const body = rows.map((r) => r.map((c) => (typeof c === "object" && c !== null && c.text !== undefined ? c : { text: String(c), options: { fontSize: fs } })));
  slide.addTable([head, ...body], {
    x, y, w, colW, fontFace: "Calibri", fontSize: fs, color: HEX.ink, valign: "middle",
    border: { type: "solid", pt: 0.75, color: HEX.hair }, fill: { color: HEX.white },
    margin: [3, 6, 3, 6], rowH: opts.rowH,
  });
}

function notes(slide, str) {
  slide.addNotes(str.replace(/\s+/g, " ").trim());
}

module.exports = { pres, THEME, HEX, C, W, M, icon, chip, content, card, text, bullets, pill, iconRow, table, notes, OUT };

// Slides are added in slides.js, so this file stays the place for the frame.
if (require.main === module) {
  require(path.join(__dirname, "slides.js"))
    .build(module.exports)
    .then(() => pres.writeFile({ fileName: OUT }))
    .then((f) => console.log("wrote", f))
    .catch((e) => { console.error(e); process.exit(1); });
}
