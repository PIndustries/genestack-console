import { esc } from "../api.js";

const BREADCRUMBS = new Map();

export function setBreadcrumbs(crumbArray) {
  BREADCRUMBS.set(location.hash || "#/", crumbArray);
  renderBreadcrumbs();
}

export function renderBreadcrumbs() {
  let el = document.getElementById("breadcrumbs-bar");
  if (!el) {
    el = document.createElement("div");
    el.id = "breadcrumbs-bar";
    const page = document.getElementById("page-root");
    if (page) {
      page.insertBefore(el, page.firstChild);
    }
  }
  const hash = location.hash || "#/";
  const crumbs = BREADCRUMBS.get(hash);
  if (!crumbs || !crumbs.length) {
    el.innerHTML = "";
    return;
  }
  let html = '<nav class="crumbs">';
  crumbs.forEach((crumb, i) => {
    const isLast = i === crumbs.length - 1;
    const label = esc(crumb.label);
    if (crumb.href && !isLast) {
      html += `<a href="${esc(crumb.href)}" style="color:var(--fg,#ccc);text-decoration:none">${label}</a> <span style="margin:0 .2rem">/</span> `;
    } else {
      html += label;
    }
  });
  html += '</nav>';
  el.innerHTML = html;
}

// Listen for hash changes to update breadcrumbs
window.addEventListener("hashchange", renderBreadcrumbs);
