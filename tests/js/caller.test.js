// Caller screen unit tests under Node.js (Structure Change v3).
// Tests for:
// - live queue list rendering
// - highlighted first row with NEXT action button
// - absence of NEXT action button on non-first rows
// - faculty CSS class and palette styling
// - NEXT button click handler
// - empty queue state
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const { buildRow, renderQueue, formatFaculty } = require(path.join(__dirname, "..", "..", "static", "caller.js"));

function createFakeDoc() {
  function makeEl(tagName) {
    const el = {
      tagName: tagName.toUpperCase(),
      className: "",
      id: "",
      textContent: "",
      hidden: false,
      disabled: false,
      dataset: {},
      style: {
        borderLeftColor: "",
        backgroundColor: "",
        color: "",
        properties: {},
        setProperty(name, val) {
          this.properties[name] = val;
        }
      },
      children: [],
      appendChild(child) {
        this.children.push(child);
        return child;
      },
      replaceChild(newChild, oldChild) {
        const idx = this.children.indexOf(oldChild);
        if (idx !== -1) {
          this.children[idx] = newChild;
        }
        return newChild;
      },
      contains(child) {
        return this.children.includes(child);
      },
      setAttribute(name, val) {
        this[name] = val;
      },
      querySelector(selector) {
        if (selector === ".cq-btn-next") {
          return this._findChild((c) => c.className && c.className.includes("cq-btn-next"));
        }
        return null;
      },
      _findChild(predicate) {
        for (const child of this.children) {
          if (predicate(child)) return child;
          if (child._findChild) {
            const found = child._findChild(predicate);
            if (found) return found;
          }
        }
        return null;
      }
    };
    return el;
  }

  return {
    createElement: makeEl
  };
}

const sampleStudents = [
  {
    student_id: "s-1",
    name: "Aaditya Patil",
    prn: "PRN001",
    programme: "B.Tech Computer Science",
    school: "Engineering & Technology",
    faculty: "ENGINEERING",
    queue_position: 1,
    palette: { strong: "#134B90", light: "#9EC8E9" }
  },
  {
    student_id: "s-2",
    name: "Neha Deshmukh",
    prn: "PRN002",
    programme: "B.Sc Chemistry",
    school: "Basic and Applied Sciences",
    faculty: "SCIENCE",
    queue_position: 2,
    palette: { strong: "#278844", light: "#9DD29C" }
  },
  {
    student_id: "s-3",
    name: "Rohan Verma",
    prn: "PRN003",
    programme: "BBA",
    school: "Management and Commerce",
    faculty: "MANAGEMENT",
    queue_position: 3,
    palette: { strong: "#CB3127", light: "#F8B0AC" }
  }
];

test("formatFaculty resolves known and fallback faculty names", () => {
  assert.equal(formatFaculty("ENGINEERING"), "Engineering");
  assert.equal(formatFaculty("SCIENCE"), "Science");
  assert.equal(formatFaculty("MANAGEMENT"), "Management");
  assert.equal(formatFaculty("UNMAPPED"), "General");
  assert.equal(formatFaculty(""), "General");
});

test("first row is highlighted with cq-row--first and includes NEXT button", () => {
  const doc = createFakeDoc();
  const row = buildRow(sampleStudents[0], true, 1, doc);

  assert.ok(row.className.includes("cq-row--first"), "First row must have cq-row--first class");
  assert.ok(row.className.includes("cq-faculty-engineering"), "Row must have faculty class");
  assert.equal(row.id, "cq-row-s-1");
  assert.equal(row.dataset.studentId, "s-1");

  const btn = row.querySelector(".cq-btn-next");
  assert.ok(btn != null, "First row must have a NEXT button");
  assert.equal(btn.className, "cq-btn-next");
});

test("subsequent rows do NOT have cq-row--first and do NOT have a NEXT button", () => {
  const doc = createFakeDoc();
  const row2 = buildRow(sampleStudents[1], false, 2, doc);

  assert.ok(!row2.className.includes("cq-row--first"), "Second row must NOT have cq-row--first");
  assert.ok(row2.className.includes("cq-faculty-science"), "Second row must have faculty class");
  const btn2 = row2.querySelector(".cq-btn-next");
  assert.equal(btn2, null, "Second row must not have NEXT button");
});

test("faculty palette styling is applied to row and badges", () => {
  const doc = createFakeDoc();
  const row = buildRow(sampleStudents[0], true, 1, doc);

  assert.equal(row.style.borderLeftColor, "#134B90");
  assert.equal(row.style.properties["--cq-fac-strong"], "#134B90");
  assert.equal(row.style.properties["--cq-fac-light"], "#9EC8E9");
});

test("renderQueue populates list container, updates count, and sets emptyEl", () => {
  const doc = createFakeDoc();
  const list = doc.createElement("ul");
  const countEl = doc.createElement("span");
  const emptyEl = doc.createElement("p");
  emptyEl.hidden = false;

  let calledStudentId = null;
  const onCall = (sid) => { calledStudentId = sid; };

  // Render list of 3 students
  renderQueue(sampleStudents, 3, { list, countEl, emptyEl }, onCall, doc);

  assert.equal(countEl.textContent, 3);
  assert.equal(emptyEl.hidden, true);
  assert.equal(list.children.length, 3);

  // First child has button and clicking it calls callback
  const firstChild = list.children[0];
  const nextBtn = firstChild.querySelector(".cq-btn-next");
  assert.ok(nextBtn != null);
  nextBtn.onclick();
  assert.equal(calledStudentId, "s-1");

  // Empty queue
  renderQueue([], 0, { list, countEl, emptyEl }, onCall, doc);
  assert.equal(countEl.textContent, 0);
  assert.equal(emptyEl.hidden, false);
});
