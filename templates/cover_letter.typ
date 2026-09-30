// Plain one-page cover letter. Content arrives as JSON via sys.inputs.data (strings are never
// evaluated as markup). Embedded fonts only, for identical output on Linux and Windows.
#let d = json(bytes(sys.inputs.data))

#set document(title: d.name + " - Cover Letter", author: d.name)
#set page(paper: "us-letter", margin: 1in)
#set text(font: ("Libertinus Serif", "New Computer Modern"), size: 11pt, hyphenate: false,
          lang: "en")
#set par(justify: false, leading: 0.6em, spacing: 1.1em)

#let piece(c) = if c.url != "" { link(c.url, c.text) } else { [#c.text] }

#text(size: 16pt, weight: "bold", d.name)
#if d.contact.len() > 0 [ \ #d.contact.map(piece).join([#h(0.5em)|#h(0.5em)])]

#v(1.2em)
#d.date

#if d.recipient.len() > 0 [#d.recipient.map(r => [#r]).join(linebreak())]

#d.greeting

#for p in d.paragraphs {
  [#p]
  parbreak()
}

#d.closing \
#d.name
