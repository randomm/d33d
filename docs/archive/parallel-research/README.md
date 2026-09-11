# Parallel research — ABANDONED direction

> **WARNING — not the spec of record.**
>
> This directory documents an **ABANDONED** exploration of
> diffusion-on-Apple-Silicon for 3D asset generation (TRELLIS.2,
> Hunyuan3D, TripoSR, InstantMesh, Stable Fast 3D — see
> [`content.md`](content.md) for the model-by-model matrix and
> [`out.md`](out.md) for the full research report;
> [`trun_0d4be9fd72b7476c82cc9959449aaee6.json`](trun_0d4be9fd72b7476c82cc9959449aaee6.json)
> is the raw tool run that produced the report).
>
> The direction was abandoned in favour of the **OpenSCAD-parametric**
> design loop described in [`docs/backlog/`](../../backlog/). Those
> backlog tickets are the spec of record for this project.
>
> Do not read this directory as a design decision. Its value is as a
> record of why the diffusion route was evaluated and set aside
> (Docker Desktop on macOS gives no Metal/MPS passthrough; verified
> Apple ports are minutes-per-asset, not seconds; the CUDA-sensitive
> surface — `flash-attn`, `xformers`, `nvdiffrast`, `spconv`,
> `cumesh`, custom rasterizers — has no clean MPS/MLX path for the
> models we considered).
