// The categorical hues every chart on this server draws from, in fixed order:
// a series keeps its colour when a filter drops the ones beside it.
//
// Validated as a set against the dark chart surface (#191c24) with the
// dataviz skill's validator: worst adjacent pair is amber/aqua at CVD dE 8.4
// and normal-vision dE 19.3. Re-run it before changing any value here, and
// keep the count at six -- a seventh hue is what TRACE_LIMIT folds into Other.
window.CCREPORT_COLORS = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300"];
