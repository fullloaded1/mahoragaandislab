# Lead Qualification Skills

Harvey uses a hybrid qualification approach: BANT for initial screening, enhanced with ICP scoring for prioritization.

---

## BANT Framework (Initial Qualification)

### Budget
- Can they afford the solution?
- Signals: company size, funding stage, job postings for related roles
- Green flags: recently funded, hiring in your product's domain
- Red flags: very small team with no budget indicators

### Authority
- Is this person a decision-maker or influencer?
- Decision-maker titles: VP, Director, Head of, C-suite, Founder
- Influencer titles: Manager, Lead, Senior — can champion internally
- If not the authority: ask for the right person (don't waste sequences)

### Need
- Do they have the problem you solve?
- Signals: tech stack, industry challenges, LinkedIn posts about pain points
- Validate by: checking their current tools, recent job posts, company stage

### Timeline
- Are they likely to buy soon?
- Signals: active evaluation (G2/Capterra reviews), recent vendor changes
- "Not now" is okay — schedule a future touchpoint

---

## ICP Scoring System

Score every prospect 1-10 before outreach:

| Criteria | Points | Details |
|----------|--------|---------|
| **Title match** | 0-3 | Exact ICP title = 3, adjacent = 2, related = 1 |
| **Company fit** | 0-3 | Right industry + size = 3, one match = 2, tangential = 1 |
| **Engagement signals** | 0-2 | Active on LinkedIn = 1, posts about relevant topics = 2 |
| **Reachability** | 0-2 | Verified email = 2, LinkedIn only = 1, no contact = 0 |

### Priority Tiers
- **Score 8-10**: High priority — personalized outreach immediately
- **Score 5-7**: Medium priority — include in batch campaigns
- **Score 3-4**: Low priority — only if pipeline needs volume
- **Score 1-2**: Skip — not worth the outreach

---

## MEDDIC (For Complex Deals — Future Use)

When Harvey handles enterprise prospects or multi-stakeholder deals:

- **Metrics**: What quantifiable outcomes does the buyer care about?
- **Economic Buyer**: Who controls the budget?
- **Decision Criteria**: What factors will they use to decide?
- **Decision Process**: What steps do they go through to buy?
- **Identify Pain**: What's the core problem driving urgency?
- **Champion**: Is there an internal advocate for your solution?

---

## Disqualification Signals

Harvey should NOT pursue prospects who:
- Have fewer employees than the minimum ICP threshold
- Are in a completely unrelated industry
- Have no discoverable contact information
- Already use a deeply entrenched competitor (unless switching signals exist)
- Explicitly opted out of communications

## Enrichment Priorities

When qualifying, gather in this order:
1. Company domain + size (free: LinkedIn, website)
2. Decision-maker name + title (free: LinkedIn search)
3. Email address (free: pattern guessing + SMTP verification)
4. Recent company news (free: Google search)
5. Tech stack (free: BuiltWith, Wappalyzer)
6. Personalization angles (free: LinkedIn posts, company blog)

---

## Lab Procurement Qualification & Human Handoff (RevOps Adaptation)

### Lab Procurement Scoring Signals (Indonesian Context)

Score prospects higher when they show verifiable institutional buying signals:
- **Universitas / Politeknik**: New study program accreditation, faculty research grant disbursement (hibah Dikti/BRIN), or practical lab refurbishment. Key titles: Kepala Laboratorium, Dosen Pembina Lab, Dekan/Wakil Dekan II (Keuangan/Sarpras).
- **Rumah Sakit / Lab Medis**: Hospital accreditation preparation (KARS), expansion of clinical pathology/microbiology units, or autoclave sterilization upgrading (CSSD). Key titles: Kepala Instalasi CSSD, Penanggung Jawab Lab, Manajer Penunjang Medis.
- **Pabrik / Industri QC**: Factory expansion, ISO 17025 renewal, BPOM audit readiness, or frequent testing volume increases. Key titles: QC Manager, QA Head, Lab Supervisor, Purchasing/Procurement Lead.
- **Instansi Pemerintah / Riset**: Year-end budget absorption timeline (September–November), annual APBN/APBD procurement planning (January–March), or public tenders on LPSE. Key titles: PPK (Pejabat Pembuat Komitmen), Tim Pengadaan, Kasubbag Umum/Sarpras.

### Strict Human Handoff Triggers (When Harvey Steps Aside)

Harvey is strictly an AI conversational assistant and must immediately pause automated interaction and hand off to a human sales engineer / AE when any of these triggers occur:
1. **Formal SPH / Quotation Request**: The prospect requests an official stamped quotation (Surat Penawaran Harga), detailed spec sheet, or formal bill of quantities.
2. **Tender & e-Katalog Engagement**: The inquiry enters formal procurement procedures (LPSE tender schedule, e-Katalog ID creation, or SPK draft review).
3. **Site Visit / Technical Demo**: The prospect requests an on-site visit for lab dimension measurement, electrical/utility assessment, or physical product demonstration.
4. **Commercial Negotiation**: Any request for custom discount structures, extended payment terms (TOP 30/60), or non-standard warranty clauses.
5. **Human Preference / Frustration**: The prospect explicitly asks to speak to a person, or expresses irritation or confusion.

### Handoff Protocol
- Flag contact status as `NEEDS_HUMAN_ATTENTION` in state/CRM.
- Immediately stop all automated sequences for that contact.
- Provide the assigned human sales engineer with:
  1. Contact name, title, and organization.
  2. Stated lab equipment need and target timeline.
  3. Verifiable context notes (procurement channel, accreditation cycle, or current pain points).
