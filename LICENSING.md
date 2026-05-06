# Licensing

**AlphaToRoto is licensed under GPL-3.0.** The full text is in the [`LICENCE`](./LICENCE) file at the root of the repository — that file is authoritative; this document is just a plain-language summary of what GPL-3.0 means for the people who actually use the plugin.

The headline: **the GPL controls redistribution of the plugin itself, not the work you produce with it.** You can use AlphaToRoto in any pipeline — commercial or non-commercial — including paid client work, broadcast, theatrical, advertising, and games, with no obligation to release anything you make with it.

## What you can do freely

- **Use it commercially.** Trace mattes, deliver shots, bill the client. The roto geometry, the rendered frames, and any downstream output belong entirely to you. They are not "derivative works" of the plugin in any copyright-meaningful sense — same way GIMP being GPL does not make your photos GPL.
- **Deploy it across a studio.** Installing AlphaToRoto on every workstation in your facility is internal use, not distribution, and triggers no GPL obligation.
- **Modify it for your pipeline.** Patch it, rebuild it, fork it. As long as the modified binary stays inside your organisation, you owe no one anything.
- **Redistribute it unmodified**, provided you keep the licence and copyright notices intact and pass the recipients the same rights you received.

## What requires extra steps

- **Distributing a modified binary outside your organisation** — to a co-production partner, a vendor, a freelancer, the public — triggers GPL: you must offer the recipient the corresponding source code under GPL-3.0 terms.
- **Bundling AlphaToRoto into a closed-source product** is not permitted under GPL-3.0 alone, and would additionally require a commercial potrace licence (see below). If you want to do this, contact me directly.

## For studio legal review

> AlphaToRoto is GPL-3.0 licensed and links statically against potrace, which is licensed under "GPL version 2 or, at your option, any later version" (GPL-2.0-or-later) with a separate commercial licence available. AlphaToRoto exercises the "or later" option in potrace's licence to relicense the combined work under GPL-3.0; the resulting combined work is therefore consistently GPL-3.0. The plugin may be used freely in any pipeline — commercial or non-commercial — including for paid client work; GPL-3.0 controls redistribution of the plugin itself, not the images, geometry, or other output produced with it. Studios may install, deploy, and internally modify the plugin without triggering any source-release obligation. Redistribution of a modified binary outside the organisation, or bundling into a closed-source product, requires GPL compliance and (for closed-source bundling) a commercial potrace licence from Peter Selinger.

## potrace

AlphaToRoto embeds [potrace](https://potrace.sourceforge.net/) by Peter Selinger as a statically linked third-party dependency. potrace is dual-licensed under "GPL version 2 or, at your option, any later version" (GPL-2.0-or-later) and a commercial licence available from the author. AlphaToRoto exercises the "or later" option to combine potrace under GPL-3.0 terms — this is the standard, legally clean way to mix GPL-2.0+ code into a GPL-3.0 work, and it is why you will see GPL-2.0 license text in `third_party/potrace/COPYING` while the combined plugin is released as GPL-3.0.

The "Potrace" name is Peter Selinger's trademark and is used here only for attribution. AlphaToRoto is not a fork or rebrand of Potrace; it embeds the potrace library as a dependency.

If you want to embed AlphaToRoto (or a derivative of it) in a closed-source product, you will need to obtain a commercial potrace licence directly from Peter Selinger, in addition to negotiating with me about AlphaToRoto's own licensing.

## Commercial / non-GPL enquiries

If GPL-3.0 does not fit your use case — for example, you want to ship a closed-source pipeline tool that includes AlphaToRoto's tracing logic — get in touch:

- Website: [petermercell.com](https://petermercell.com)
- Patreon: [patreon.com/PeterMercell](https://patreon.com/cw/PeterMercell)

Note that for a closed-source bundling arrangement I would also need to point you at Peter Selinger for the potrace side; the two licences are independent.

---

*This document is an informational summary. The [`LICENCE`](./LICENCE) file (full GPL-3.0 text plus AlphaToRoto's project header and third-party notices) is the legally binding document. If your legal team has questions this summary does not answer, the safe answer is always to read the LICENCE file and, where in doubt, ask.*
