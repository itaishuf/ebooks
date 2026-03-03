---
name: frontend-design
description: Create distinctive, production-grade frontend interfaces with high design quality. Use this skill when the user asks to build web components, pages, artifacts, posters, or applications (examples include websites, landing pages, dashboards, React components, HTML/CSS layouts, or when styling/beautifying any web UI). Generates creative, polished code and UI design that avoids generic AI aesthetics.
license: Complete terms in LICENSE.txt
---

This skill guides creation of distinctive, production-grade frontend interfaces that avoid generic "AI slop" aesthetics. Implement real working code with exceptional attention to aesthetic details and creative choices.

The user provides frontend requirements: a component, page, application, or interface to build. They may include context about the purpose, audience, or technical constraints.

## Design Thinking

Before coding, understand the context and commit to a BOLD aesthetic direction:

- **Purpose**: What problem does this interface solve? Who uses it?
- **Tone**: Pick an extreme: brutally minimal, maximalist chaos, retro-futuristic, organic/natural, luxury/refined, playful/toy-like, editorial/magazine, brutalist/raw, art deco/geometric, soft/pastel, industrial/utilitarian, etc. There are so many flavors to choose from. Use these for inspiration but design one that is true to the aesthetic direction.
- **Constraints**: Technical requirements (framework, performance, accessibility).
- **Differentiation**: What makes this UNFORGETTABLE? What's the one thing someone will remember?

**CRITICAL**: Choose a clear conceptual direction and execute it with precision. Bold maximalism and refined minimalism both work - the key is intentionality, not intensity.

Then implement working code (HTML/CSS/JS, React, Vue, etc.) that is:

- Production-grade and functional
- Visually striking and memorable
- Cohesive with a clear aesthetic point-of-view
- Meticulously refined in every detail

## Frontend Aesthetics Guidelines

Focus on:

- **Typography**: Choose fonts that are beautiful, unique, and interesting. Avoid generic fonts like Arial and Inter; opt instead for distinctive choices that elevate the frontend's aesthetics; unexpected, characterful font choices. Pair a distinctive display font with a refined body font.
- **Color & Theme**: Commit to a cohesive aesthetic. Use CSS variables for consistency. Dominant colors with sharp accents outperform timid, evenly-distributed palettes.
- **Motion**: Use animations for effects and micro-interactions. Prioritize CSS-only solutions for HTML. Use Motion library for React when available. Focus on high-impact moments: one well-orchestrated page load with staggered reveals (animation-delay) creates more delight than scattered micro-interactions. Use scroll-triggering and hover states that surprise.
- **Spatial Composition**: Unexpected layouts. Asymmetry. Overlap. Diagonal flow. Grid-breaking elements. Generous negative space OR controlled density.
- **Backgrounds & Visual Details**: Create atmosphere and depth rather than defaulting to solid colors. Add contextual effects and textures that match the overall aesthetic. Apply creative forms like gradient meshes, noise textures, geometric patterns, layered transparencies, dramatic shadows, decorative borders, custom cursors, and grain overlays.

NEVER use generic AI-generated aesthetics like overused font families (Inter, Roboto, Arial, system fonts), cliched color schemes (particularly purple gradients on white backgrounds), predictable layouts and component patterns, and cookie-cutter design that lacks context-specific character.

Interpret creatively and make unexpected choices that feel genuinely designed for the context. No design should be the same. Vary between light and dark themes, different fonts, different aesthetics. NEVER converge on common choices (Space Grotesk, for example) across generations.

**IMPORTANT**: Match implementation complexity to the aesthetic vision. Maximalist designs need elaborate code with extensive animations and effects. Minimalist or refined designs need restraint, precision, and careful attention to spacing, typography, and subtle details. Elegance comes from executing the vision well.

Remember: Claude is capable of extraordinary creative work. Don't hold back, show what can truly be created when thinking outside the box and committing fully to a distinctive vision.

## This web app design language

meta:
  name: "Cozy Watercolor Storybook UI"
  version: "1.1.0"
  goal: "Generate cohesive watercolor storybook-style UI visuals for a mobile app"

style_principles:
  core_feel:
    - "Cozy"
    - "Warm"
    - "Intimate"
    - "Hand-painted"
    - "Soft and organic"
  avoid:
    - "Flat vector style"
    - "Neon colors"
    - "Hard outlines"
    - "Photorealism"
    - "3D rendering"
    - "High contrast tech aesthetic"

medium:
  type: "Watercolor on cold-pressed paper"
  texture:
    - "Visible paper grain"
    - "Soft pigment granulation"
    - "Layered translucent washes"
    - "Subtle watercolor blooms"
  line_quality:
    - "Minimal harsh strokes"
    - "Slightly imperfect hand-drawn edges"
    - "Painted details instead of inked outlines"

lighting:
  primary: "Warm ambient glow (like soft lamplight)"
  secondary: "Soft diffused daylight"
  constraints:
    - "No dramatic rim light"
    - "No strong shadow contrast"
    - "No glossy highlights"

color_system:
  palette:
    - name: "Burnt Orange"
      hex: "#C56A2A"
    - name: "Pumpkin"
      hex: "#E08A3B"
    - name: "Warm Umber"
      hex: "#5B3A29"
    - name: "Moss Green"
      hex: "#556B3C"
    - name: "Cream Paper"
      hex: "#F3E8D5"
    - name: "Charcoal"
      hex: "#2A2A2A"
    - name: "Dusty Blue Accent"
      hex: "#6E86A6"
  rules:
    - "Use Cream Paper as default background base"
    - "Primary accents: Burnt Orange / Pumpkin"
    - "Secondary accents: Moss Green / Dusty Blue"
    - "Text color: Charcoal (never pure black)"
    - "Avoid saturated primary RGB colors"

layout_principles:
  spacing:
    - "Generous padding"
    - "Comfortable breathing room"
    - "No crowded layouts"
  shapes:
    - "Rounded corners (12–20px)"
    - "Organic silhouettes"
    - "Soft edges"
  depth:
    - "Subtle drop shadows"
    - "Layered watercolor washes"
    - "Paper cutout feel"
    - "No glassmorphism"
    - "No sharp elevation jumps"

ui_components:
  buttons:
    primary:
      shape: "Rounded pill or soft rectangle"
      fill: "Watercolor wash (Burnt Orange / Pumpkin)"
      text: "Cream Paper"
      shadow: "Soft diffuse shadow"
    secondary:
      fill: "Light watercolor wash"
      border: "Soft painted edge"
  cards:
    style:
      - "Paper texture background"
      - "Soft shadow"
      - "Gentle color wash"
      - "Rounded corners"
  icons:
    style:
      - "Hand-drawn watercolor"
      - "Minimal detail"
      - "Consistent softness"
      - "No thick outlines"
  dividers:
    - "Soft brush stroke line"
    - "Subtle leaf/vine accent (optional, minimal)"

typography:
  tone: "Warm, human, readable"
  rules:
    - "Use humanist sans-serif for body text"
    - "Optional soft serif for headings"
    - "High readability priority"
    - "Avoid decorative childish fonts"
  text_color: "#2A2A2A"

background_generation_rules:
  description_style: >
    Hand-painted watercolor illustration on textured cold-pressed paper,
    warm earthy palette, soft ambient glow, cozy intimate atmosphere,
    no harsh outlines, no 3D, no neon, no flat vector design.
  composition_rules:
    - "Leave intentional negative space for UI overlay"
    - "Keep center area clean when needed"
    - "Use subtle vignette lighting"

image_prompt_templates:
  global_prefix: >
    Hand-painted watercolor illustration on textured cold-pressed paper,
    warm earthy autumn palette, soft gentle lighting, cozy intimate mood,
    organic shapes, no harsh outlines, no photorealism, no 3D, no neon colors.
  negative_prompt: >
    photorealistic, 3D render, neon colors, flat vector, hard outline,
    high contrast lighting, glossy UI, cyberpunk, glassmorphism.

qa_visual_checklist:

- "Does it feel hand-painted?"
- "Are colors warm and earthy?"
- "Is lighting soft and diffused?"
- "Are components rounded and organic?"
- "Is readability preserved?"
- "Are there no harsh digital effects?"

agent_execution_rules:

- "Only generate visual and UI-related output."
- "Do not define product logic, features, flows, or user interactions."
- "Focus strictly on visual system consistency."
- "Maintain aesthetic coherence across all generated assets."
