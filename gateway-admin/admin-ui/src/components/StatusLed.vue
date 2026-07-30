<!-- src/components/StatusLed.vue -->
<template>
  <span class="led" :class="[status, pulse ? 'pulse' : '']"></span>
</template>

<script setup>
defineProps({
  status: { type: String, default: 'ok' },
  pulse: { type: Boolean, default: false },
})
</script>

<style scoped>
/* ── Extracted from docs/superpowers/mockups/gateway-admin.html ── */
.led { display: inline-block; width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; position: relative; }
.led.ok   { background: var(--ok);   box-shadow: 0 0 8px var(--ok); }
.led.warn { background: var(--warn); box-shadow: 0 0 8px var(--warn); }
.led.err  { background: var(--err);  box-shadow: 0 0 8px var(--err); }
.led.off  { background: var(--faint); }
.led.pulse::after {
  content: ""; position: absolute; inset: -4px;
  border-radius: 50%; border: 1.5px solid currentColor;
  opacity: 0; animation: led-ring 2.2s ease-out infinite;
}
.led.ok.pulse { color: var(--ok); }
.led.warn.pulse { color: var(--warn); }
.led.err.pulse { color: var(--err); }
@keyframes led-ring { 0% { transform: scale(0.5); opacity: 0.7; } 70% { transform: scale(1.7); opacity: 0; } 100% { opacity: 0; } }
</style>
