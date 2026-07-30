document.getElementById("year").textContent = new Date().getFullYear();

const SECTIONS = ["model", "security", "harness"];
const READ_KEY_PREFIX = "simplenews:read:";

function relativeTime(iso) {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  const now = Date.now();
  const diffSec = Math.max(0, Math.round((now - then) / 1000));
  const units = [
    ["y", 31536000],
    ["mo", 2592000],
    ["d", 86400],
    ["h", 3600],
    ["m", 60],
  ];
  for (const [label, secs] of units) {
    if (diffSec >= secs) return `${Math.floor(diffSec / secs)}${label} ago`;
  }
  return "just now";
}

function scoreLabel(item) {
  if (item.source === "reddit") return `${item.score}up`;
  if (item.source === "hn") return `${item.score}pt`;
  return null;
}

function isRead(url) {
  try {
    return localStorage.getItem(READ_KEY_PREFIX + url) !== null;
  } catch {
    return false;
  }
}

function markRead(url) {
  try {
    localStorage.setItem(READ_KEY_PREFIX + url, Date.now().toString());
  } catch {
    /* localStorage unavailable (private mode, etc) - read-state just won't persist */
  }
}

function truthinessTitle(t) {
  const labelText = { green: "truthy", yellow: "neutral", red: "untruthy" }[t.label] || t.label;
  return `Truthiness: ${labelText}. ${t.reason} See methodology.html for how this is scored.`;
}

function renderEntry(item) {
  const li = document.createElement("li");
  li.className = "entry";
  if (isRead(item.url)) li.classList.add("is-read");

  const titleRow = document.createElement("div");
  titleRow.className = "entry-title-row";

  const gutter = document.createElement("span");
  gutter.className = "truth-gutter";
  if (item.truthiness) {
    const dot = document.createElement("span");
    dot.className = `truth-dot truth-${item.truthiness.label}`;
    dot.title = truthinessTitle(item.truthiness);
    dot.tabIndex = 0;
    dot.setAttribute("role", "img");
    dot.setAttribute("aria-label", truthinessTitle(item.truthiness));
    gutter.appendChild(dot);
  }
  titleRow.appendChild(gutter);

  const a = document.createElement("a");
  a.className = "entry-title";
  a.href = item.url;
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  a.textContent = item.title;
  a.addEventListener("click", () => {
    markRead(item.url);
    li.classList.add("is-read");
  });
  titleRow.appendChild(a);

  li.appendChild(titleRow);

  const meta = document.createElement("p");
  meta.className = "entry-meta";

  const tag = document.createElement("span");
  tag.className = "tag";
  tag.textContent = item.source;
  meta.appendChild(tag);

  const score = scoreLabel(item);
  if (score) {
    const s = document.createElement("span");
    s.textContent = score;
    meta.appendChild(s);
  }

  const time = document.createElement("span");
  time.textContent = relativeTime(item.published);
  meta.appendChild(time);

  li.appendChild(meta);
  return li;
}

function renderSection(name, items) {
  const list = document.getElementById(`${name}-entries`);
  list.innerHTML = "";
  const existingState = list.parentElement.querySelector(".empty-state, .error-state");
  if (existingState) existingState.remove();

  if (!items || items.length === 0) {
    const p = document.createElement("p");
    p.className = "empty-state";
    p.textContent = "no fresh signal, check back later";
    list.parentElement.appendChild(p);
    return;
  }
  for (const item of items) {
    list.appendChild(renderEntry(item));
  }
}

function wireFilters() {
  document.querySelectorAll(".filter").forEach((input) => {
    input.addEventListener("input", () => {
      const target = input.dataset.target;
      const query = input.value.trim().toLowerCase();
      const list = document.getElementById(`${target}-entries`);
      for (const li of list.children) {
        const title = li.querySelector(".entry-title")?.textContent.toLowerCase() ?? "";
        li.hidden = query.length > 0 && !title.includes(query);
      }
    });
  });
}

function dataUrlFor(dateParam) {
  return dateParam ? `data/archive/${dateParam}.json` : "data/news.json";
}

function shortDate(iso) {
  const [, m, d] = iso.split("-");
  return `${m}/${d}`;
}

function renderHistory(days) {
  const chart = document.getElementById("history-chart");
  chart.innerHTML = "";
  if (!days || days.length === 0) {
    chart.innerHTML = '<p class="empty-state">no history yet</p>';
    return;
  }

  const maxTotal = Math.max(1, ...days.map((d) => SECTIONS.reduce((sum, s) => sum + (d.counts[s] || 0), 0)));

  for (const day of days) {
    const total = SECTIONS.reduce((sum, s) => sum + (day.counts[s] || 0), 0);
    const a = document.createElement("a");
    a.className = "history-day";
    a.href = `index.html?date=${encodeURIComponent(day.date)}`;
    a.title = `${day.date}: ${SECTIONS.map((s) => `${day.counts[s] || 0} ${s}`).join(", ")}`;

    if (total === 0) {
      const empty = document.createElement("div");
      empty.className = "history-empty";
      a.appendChild(empty);
    } else {
      // Reverse order so the segment stack reads top-to-bottom as
      // model/security/harness, matching the column order above, since
      // flex-direction: column-reverse isn't used here (day bars grow
      // from the bottom via justify-content: flex-end instead).
      for (const section of [...SECTIONS].reverse()) {
        const count = day.counts[section] || 0;
        if (count === 0) continue;
        const seg = document.createElement("div");
        seg.className = `history-segment ${section}`;
        seg.style.height = `${(count / maxTotal) * 100}%`;
        a.appendChild(seg);
      }
    }

    chart.appendChild(a);
  }

  const labels = document.getElementById("history-labels");
  if (labels) {
    labels.innerHTML = "";
    const first = document.createElement("span");
    first.textContent = shortDate(days[0].date);
    const last = document.createElement("span");
    last.textContent = shortDate(days[days.length - 1].date);
    labels.append(first, last);
  }
}

async function loadHistory() {
  try {
    const res = await fetch("data/history.json", { cache: "no-store" });
    if (!res.ok) throw new Error(`fetch failed: ${res.status}`);
    const data = await res.json();
    renderHistory(data.days);
  } catch (err) {
    const chart = document.getElementById("history-chart");
    if (chart) chart.innerHTML = '<p class="empty-state">history unavailable</p>';
    console.error(err);
  }
}

async function main() {
  const updatedLine = document.getElementById("updated-line");
  const archiveBanner = document.getElementById("archive-banner");
  const dateParam = new URLSearchParams(window.location.search).get("date");

  loadHistory();

  if (dateParam) {
    archiveBanner.hidden = false;
    archiveBanner.innerHTML = `viewing archived snapshot for ${dateParam} - <a href="index.html">view latest</a>`;
  }

  try {
    const res = await fetch(dataUrlFor(dateParam), { cache: "no-store" });
    if (!res.ok) throw new Error(`fetch failed: ${res.status}`);
    const data = await res.json();

    for (const name of SECTIONS) {
      renderSection(name, data.sections?.[name] ?? []);
    }

    updatedLine.textContent = data.generated_at
      ? `last synced ${relativeTime(data.generated_at)}`
      : "";

    wireFilters();
  } catch (err) {
    updatedLine.textContent = "sync unavailable";
    for (const name of SECTIONS) {
      const list = document.getElementById(`${name}-entries`);
      const p = document.createElement("p");
      p.className = "error-state";
      p.textContent = "could not load feed data";
      list.parentElement.appendChild(p);
    }
    console.error(err);
  }
}

main();
