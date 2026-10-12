<template>
  <div ref="chartRef" :style="{ width: '100%', height: height + 'px' }"></div>
</template>

<script setup>
import { ref, watch, onMounted, onBeforeUnmount } from 'vue'
import * as echarts from 'echarts'

const props = defineProps({
  indicators: { type: Array, default: () => [] }, // [{name, max}]
  series: { type: Array, default: () => [] },     // [{name, values: [], color?}]
  height: { type: Number, default: 360 },
})

const chartRef = ref(null)
let chart = null
const PALETTE = ['#3E63DD', '#E8912D', '#2FA36B', '#C2418B', '#7C6BD6']

function render() {
  if (!chart || !props.indicators?.length) return
  chart.setOption({
    tooltip: { trigger: 'item' },
    legend: { bottom: 0, textStyle: { fontSize: 12 } },
    radar: {
      indicator: props.indicators,
      radius: '62%',
      splitNumber: 4,
      axisName: { color: '#666', fontSize: 12 },
    },
    series: [{
      type: 'radar',
      symbolSize: 4,
      data: props.series.map((s, i) => ({
        name: s.name,
        value: s.values,
        itemStyle: { color: s.color || PALETTE[i % PALETTE.length] },
        lineStyle: { color: s.color || PALETTE[i % PALETTE.length], width: 2 },
        areaStyle: { color: s.color || PALETTE[i % PALETTE.length], opacity: 0.12 },
      })),
    }],
  })
}

onMounted(() => {
  chart = echarts.init(chartRef.value)
  render()
  window.addEventListener('resize', onResize)
})
function onResize() { chart?.resize() }
onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  chart?.dispose()
  chart = null
})
watch(() => [props.indicators, props.series], render, { deep: true })
</script>
