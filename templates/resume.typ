// ATS-friendly, single-column resume.
// All content arrives as JSON via sys.inputs.data and is inserted as plain strings, never
// evaluated as markup, so user text needs no escaping. Only Typst's embedded fonts are used,
// so the output is identical on Linux and Windows.
#let d = json(bytes(sys.inputs.data))

#set document(title: d.name + " - Resume", author: d.name)
#set page(paper: "us-letter", margin: (x: 0.6in, y: 0.5in))
#set text(font: ("Libertinus Serif", "New Computer Modern"), size: 10.5pt, hyphenate: false,
          lang: "en")
#set par(justify: false, leading: 0.5em, spacing: 0.55em)
#set list(indent: 0.4em, body-indent: 0.45em, spacing: 0.4em, marker: [•])

#show heading.where(level: 1): it => block(
  above: 0.9em, below: 0.5em, width: 100%, stroke: (bottom: 0.5pt), inset: (bottom: 2.5pt),
  text(size: 11.5pt, weight: "bold", it.body),
)

#let sep = [#h(0.5em)|#h(0.5em)]
#let piece(c) = if c.url != "" { link(c.url, c.text) } else { [#c.text] }

// ---------------------------------------------------------------- header
#align(center)[
  #text(size: 18pt, weight: "bold", d.name)
  #if d.headline != "" [ \ #text(size: 11pt, d.headline)]
  #if d.contact.len() > 0 [ \ #d.contact.map(piece).join(sep)]
]

#let entry(e) = block(above: 0.75em, below: 0.4em, breakable: true)[
  #text(weight: "bold", e.org)#if e.right_top != "" [#h(1fr)#e.right_top]
  #if e.title != "" or e.right_bottom != "" [
    \ #emph(e.title)#if e.right_bottom != "" [#h(1fr)#e.right_bottom]
  ]
  #if e.bullets.len() > 0 {
    v(0.1em)
    list(..e.bullets.map(b => [#b]))
  }
]

#let sections = (
  summary: () => [= Summary
    #d.summary],
  experience: () => [= Experience
    #for e in d.experience { entry(e) }],
  projects: () => [= Projects
    #for e in d.projects { entry(e) }],
  education: () => [= Education
    #for e in d.education { entry(e) }],
  certifications: () => [= Certifications
    #list(..d.certifications.map(c => [#c]))],
  skills: () => [= Skills
    #d.skills.join(", ")],
)

#for name in d.order {
  let body = d.at(name)
  if body != none and body != "" and body != () {
    sections.at(name)()
  }
}
