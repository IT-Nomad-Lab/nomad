# Launch checklist

Standards: `~/nomad/docs/standards/WEBSITES.md`. Run before the first deploy and before any
big change goes live. Deploying is red tier: ask the operator first.

- [ ] `npm run build` and `npm run lint` pass (G1, G2)
- [ ] Every sitemap route returns 200 from the served `out/` (G3)
- [ ] Screenshots at 375, 768, 1280 px checked (G4)
- [ ] web-design-guidelines run, no accessibility findings (G5)
- [ ] Titles, descriptions, canonical, share image, robots, sitemap (G7)
- [ ] No placeholder text, no unconfirmed claim, no unlicensed image (section 5)
- [ ] No middleware, no `/api`, no secret in `out/` (G8)
- [ ] Contact method tested end to end by a human
- [ ] Domain, DNS and host settings written down in the project README
- [ ] Operator has said "go" for the deploy
