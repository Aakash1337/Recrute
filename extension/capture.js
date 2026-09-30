// Injected into the tab on an explicit "Save to Recrute" click. Returns what the server needs to
// parse a job posting, and nothing that belongs to your account on that site:
//   * no form controls at all (inputs carry session tokens, CSRF values, codes, passwords);
//   * no scripts except JSON-LD (JobPosting metadata); no meta tags except descriptive ones;
//   * LinkedIn's embedded <code> data only where it describes a job's apply method.
// Two structural reductions, never an arbitrary cut: (1) the sanitized page; (2) descriptive
// head metadata plus the main job content.
globalThis.recruteCapture = () => {
  const META_OK = /^(description|keywords|title|og:[\w:]+|twitter:(title|description)|citation_\w+)$/i;
  const strip = (root) => {
    root.querySelectorAll(
      'script:not([type="application/ld+json"]), style, noscript, svg, canvas, iframe, ' +
      "video, audio, picture source, link, template, object, embed, " +
      "input, textarea, select, datalist, output"
    ).forEach((e) => e.remove());
    root.querySelectorAll("meta").forEach((m) => {
      const key = m.getAttribute("name") || m.getAttribute("property") || "";
      if (!META_OK.test(key)) m.remove();
    });
    root.querySelectorAll("code").forEach((c) => {
      const t = c.textContent || "";
      if (c.id !== "applyUrl" && !/applyMethod|companyApplyUrl/.test(t)) c.remove();
    });
    root.querySelectorAll("*").forEach((e) => {
      for (const a of [...e.attributes]) {
        if (a.name === "style" || /token|csrf|session|secret|auth/i.test(a.name)) {
          e.removeAttribute(a.name);
        }
      }
    });
    return root;
  };
  const full = strip(document.documentElement.cloneNode(true)).outerHTML;
  const head = [...document.head.querySelectorAll(
    'title, meta, script[type="application/ld+json"]'
  )].filter((e) => e.tagName !== "META"
    || META_OK.test(e.getAttribute("name") || e.getAttribute("property") || ""))
    .map((e) => e.outerHTML).join("");
  const main = document.querySelector(
    'main, article, [role="main"], [class*="job-description"], [class*="jobDescription"], ' +
    '[id*="job"], #content'
  ) || document.body;
  const core = strip(main.cloneNode(true)).outerHTML;
  return {
    url: location.href,
    title: document.title,
    candidates: [full, `<html><head>${head}</head><body>${core}</body></html>`],
  };
};
