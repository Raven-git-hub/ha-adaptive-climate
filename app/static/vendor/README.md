# Vendored front-end libraries

uPlot v1.6.31 (https://github.com/leeoniya/uPlot), used by the Analysis view.
Files are the npm `dist` IIFE build (exposes the `uPlot` global; no build step):

  - uPlot.iife.min.js
  - uPlot.min.css

index.html references these directly. To update, replace both files from the
matching npm tarball (registry.npmjs.org/uplot/-/uplot-<version>.tgz, dist/)
and bump the version here.
