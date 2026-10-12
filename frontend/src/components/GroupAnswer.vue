<template>
  <a-spin :spinning="loading">
    <!-- 顶部：辩题 + 五阶段 -->
    <div class="mw-card topic-card">
      <div class="topic-head">
        <span class="mw-tag orange">无领导小组讨论 · {{ STAGES[stage].label }}</span>
        <div class="stage-tabs">
          <span
            v-for="(s, i) in STAGES" :key="i"
            class="stage-tab" :class="{ active: stage === i, done: stage > i }"
          >{{ i + 1 }}. {{ s.label }}</span>
        </div>
        <span class="mw-chip" :class="{ warning: recording }">{{ recording ? '● 录制中 ' + fmt(recSecs) : '⏱ 已进行 ' + fmt(totalSecs) }}</span>
      </div>
      <div class="topic-title">{{ topic }}</div>
      <div class="topic-hint">{{ STAGES[stage].hint }}</div>
    </div>

    <div class="group-layout">
      <!-- 左：同场候选人 -->
      <aside class="mw-card peers-card">
        <h4 class="card-title">同场候选人（{{ peers.length + 1 }} 人）</h4>
        <div class="peer-item me" :class="{ speaking: speakingName === '你' }">
          <div class="p-avatar" style="background: var(--primary);">你</div>
          <div class="p-info">
            <div class="p-name">你</div>
            <div class="p-bio">认真倾听，争取有效发言</div>
          </div>
        </div>
        <div
          v-for="p in peers" :key="p.id"
          class="peer-item" :class="{ speaking: speakingName === p.name }"
        >
          <div class="p-avatar" :style="{ background: p.color }">{{ (p.name || '?')[0] }}</div>
          <div class="p-info">
            <div class="p-name">
              {{ p.name }}
              <span class="p-style" :style="{ color: p.color }">{{ p.style }}</span>
            </div>
            <div class="p-bio">{{ p.bio }}</div>
          </div>
        </div>
        <div class="q-tip" style="margin-top:12px;">
          发言策略：陈述阶段亮明立场 → 自由讨论争取 2 次以上有效发言 → 总结控制在 1 分钟内
        </div>
      </aside>

      <!-- 中：讨论对话流 -->
      <section class="mw-card chat-card">
        <div class="chat-stream" ref="streamRef">
          <div
            v-for="(m, i) in messages" :key="i"
            class="msg-row" :class="m.who === 'moderator' ? 'sys moderator' : m.who"
          >
            <!-- 主持人控场消息（居中） -->
            <template v-if="m.who === 'moderator'">
              <div class="mod-bubble">
                <span class="mod-icon">🎙</span>
                <span class="mod-name">主持人</span>
                <span class="mod-text">{{ m.text }}</span>
                <span v-if="m.subtype === 'cold_call'" class="mod-tag">冷场点名</span>
                <span v-else-if="m.subtype === 'pull_back'" class="mod-tag">拉回议题</span>
                <span v-else-if="m.subtype === 'transition'" class="mod-tag">阶段推进</span>
              </div>
            </template>
            <template v-else>
              <div v-if="m.who === 'peer'" class="msg-avatar" :style="{ background: m.color }">{{ (m.name || '?')[0] }}</div>
              <div class="msg-body">
                <div class="msg-meta">
                  <template v-if="m.who === 'peer'">
                    {{ m.name }} · {{ m.style }}
                    <span v-if="m.interrupt" class="reply-to interrupt-tag">⚡ 抢话</span>
                    <span v-if="m.replyTo" class="reply-to">
                      ↳ {{ MODE_LABEL[m.replyMode] || '回应' }} {{ m.replyTo }}
                    </span>
                  </template>
                  <template v-else-if="m.who === 'me'">你 · {{ m.ts }}</template>
                  <template v-else>系统提示</template>
                </div>
                <div
                  class="msg-bubble" :class="[m.who, { interrupted: m.interrupt }]"
                  :style="m.who === 'peer' ? { borderLeft: '3px solid ' + m.color } : {}"
                >{{ m.text }}<span v-if="m.streaming" class="cursor">▍</span></div>
              </div>
            </template>
          </div>
          <div v-if="aiThinking" class="msg-row peer">
            <div class="msg-avatar" :style="{ background: thinkingColor || 'var(--text-3)' }">…</div>
            <div class="msg-body">
              <div class="msg-meta">{{ speakingName || '候选人' }} · {{ thinkingLabel }}</div>
              <div class="msg-bubble peer typing"><span></span><span></span><span></span></div>
            </div>
          </div>
        </div>

        <!-- 操作区 -->
        <div class="chat-input">
          <template v-if="waitingPeer">
            <span class="action-hint">其他候选人正在发言，请稍候…</span>
          </template>
          <template v-else>
            <span class="action-hint">{{ inputHint }}</span>
            <div class="input-btns">
              <a-input-search
                v-if="textMode"
                v-model:value="textInput"
                placeholder="输入你的发言，回车发送"
                enter-button="发送"
                style="flex:1; min-width:220px;"
                @search="sendText"
              />
              <a-button @click="textMode = !textMode">{{ textMode ? '切到语音' : '切到文字' }}</a-button>
              <button class="mic-btn" :class="{ recording }" @click="toggleRecord">
                <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                  <rect x="5" y="2" width="4" height="7" rx="2" fill="#fff"/>
                  <rect x="3" y="7" width="8" height="1.5" rx="0.75" fill="#fff"/>
                </svg>
                {{ recording ? '结束发言' : '举手发言' }}
              </button>
              <a-button v-if="stage < 4" type="primary" :disabled="myStageCount(STAGES[stage].key) === 0" @click="advanceStage">
                {{ stage === 0 ? '进入个人陈述'
                  : stage === 1 ? '进入自由交锋'
                  : stage === 2 ? '进入收敛共识'
                  : '进入总结陈词' }} →
              </a-button>
              <a-button
                v-else type="primary" :loading="submitting"
                :disabled="!canSubmit"
                @click="submitDiscussion"
              >
                提交讨论，生成报告 →
              </a-button>
            </div>
            <div v-if="stage === 4 && !canSubmit" class="action-hint" style="color:var(--warning);">
              请先「举手发言」给出你的总结陈词，主持人宣布结束后即可提交。
            </div>
          </template>
        </div>
      </section>

      <!-- 右：我的表现 -->
      <aside class="mw-card my-card">
        <h4 class="card-title">我的讨论表现</h4>
        <div class="metric-row"><span class="n">发言次数</span><span class="v">{{ myMessages.length }}</span></div>
        <div class="metric-row"><span class="n">发言总字数</span><span class="v">{{ totalChars }}</span></div>
        <div class="metric-row"><span class="n">当前阶段</span><span class="v">{{ STAGES[stage].label }}</span></div>
        <div class="metric-row"><span class="n">候选人发言</span><span class="v">{{ peerMessages.length }} 条</span></div>
        <div class="bar-wrap">
          <div class="metric-row" style="margin-bottom:6px;">
            <span class="n">参与度（发言/轮次）</span><span class="v">{{ participation }}%</span>
          </div>
          <div class="bar-shell"><span :style="{ width: participation + '%' }"></span></div>
        </div>
        <div class="bar-wrap">
          <div class="metric-row" style="margin-bottom:6px;">
            <span class="n">对话节奏感</span><span class="v">{{ rhythmScore }}/100</span>
          </div>
          <div class="bar-shell"><span :style="{ width: rhythmScore + '%' }"></span></div>
          <div class="rhythm-hint">{{ rhythmHint }}</div>
        </div>
        <div class="q-tip">
          群面考察：团队协作、逻辑说服、时间控制。避免打断他人，也要抓住发言窗口。
        </div>
      </aside>
    </div>
  </a-spin>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, nextTick, watch } from 'vue'
import { useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import { useFlowStore } from '@/store/flow'
import { sessionApi } from '@/api'
import { createAsrClient } from '@/utils/asr'

const props = defineProps({
  session: { type: Object, default: null },
  sessionId: { type: Number, default: null },
  questions: { type: Array, default: () => [] },
  loading: { type: Boolean, default: false },
})

const router = useRouter()
const flow = useFlowStore()

// —— 群面五阶段状态机（key 与后端 group_discussion.PHASES 同源）——
const STAGES = [
  { key: 'opening', label: '破冰开场',
    hint: '主持人开场后，用一句话简短表态或自我介绍，打破僵局。' },
  { key: 'statement', label: '个人陈述',
    hint: '每位候选人亮明立场与理由。你陈述后，其余候选人会依次自动陈述。' },
  { key: 'discussion', label: '自由交锋',
    hint: '交锋环节：候选人会针对你或其他人的具体观点认同补充、反驳或追问，注意可能有人抢话。' },
  { key: 'convergence', label: '收敛共识',
    hint: '发言转向归纳：寻找共同点、明确分歧、推动可执行结论。' },
  { key: 'summary', label: '总结陈词',
    hint: '候选人代表全组总结。轮到你时给出最终陈词，主持人宣布结束后提交报告。' },
]
const PHASE_IDX = Object.fromEntries(STAGES.map((s, i) => [s.key, i]))
// 回应方式标签（与后端 reply_mode 对齐）
const MODE_LABEL = { agree: '认同补充', refute: '反驳质疑', probe: '追问', answer: '回应', opening: '开场', statement: '陈述', converge: '归纳', summary: '总结' }

const stage = ref(0)
const messages = ref([])
const peers = ref([])
const speakingName = ref('')
const thinkingColor = ref('')
const aiThinking = ref(false)
const thinkingLabel = ref('正在思考')
const waitingPeer = ref(false)
const submitting = ref(false)
const discussionFinished = ref(false)   // 主持人宣布结束

const recording = ref(false)
const textMode = ref(false)
const textInput = ref('')
const recSecs = ref(0)
const totalSecs = ref(0)
let asrClient = null
let committedText = ''          // 已通过讯飞 final 确认、跨会话累计的文本
let liveText = ''               // 界面实时显示文本，作为未拿到 final 时的兜底
let recTimer = null
let totalTimer = null
let streamTimer = null          // 流式呈现定时器

const streamRef = ref(null)

const topic = computed(() => props.questions?.[0]?.question_content || '请讨论：AI 大规模取代初级岗位，对行业发展利大于弊还是弊大于利？')
const sqId = computed(() => props.questions?.[0]?.id)

const myMessages = computed(() => messages.value.filter(m => m.who === 'me'))
const peerMessages = computed(() => messages.value.filter(m => m.who === 'peer'))
const totalChars = computed(() => myMessages.value.reduce((s, m) => s + (m.text || '').length, 0))
const participation = computed(() => Math.min(95, myMessages.value.length * 16 + 8))
// 我在指定阶段的发言数
function myStageCount(phaseKey) {
  return myMessages.value.filter(m => (m.phaseKey || STAGES[m.stage]?.key) === phaseKey).length
}
// 总结阶段：我已陈词且主持人宣布结束，才能提交
const canSubmit = computed(() =>
  stage.value === 4 && discussionFinished.value && myStageCount('summary') > 0)

// 对话节奏感：综合发言次数 + 候选人互相呼应比例 + 锚定多样性
const rhythmScore = computed(() => {
  const my = myMessages.value.length
  const peer = peerMessages.value.length
  if (my === 0) return 0
  const peerToPeer = peerMessages.value.filter(m => m.replyTo && m.replyTo !== '你').length
  const ratio = peerToPeer / Math.max(1, peer)
  const anchored = peerMessages.value.filter(m => m.replyTo).length / Math.max(1, peer)
  const base = Math.min(50, my * 12) + Math.min(30, peer * 4) + Math.round(ratio * 12) + Math.round(anchored * 8)
  return Math.min(100, base)
})
const rhythmHint = computed(() => {
  if (rhythmScore.value >= 80) return '节奏很好，候选人间互相呼应、你来我往'
  if (rhythmScore.value >= 50) return '节奏还行，多关注场上其他候选人的观点'
  if (myMessages.value.length === 0) return '尚未发言，先「举手发言」参与讨论'
  return '发言偏少，鼓励更主动地参与讨论'
})

const inputHint = computed(() => STAGES[stage.value].hint)

function fmt(s) {
  const m = String(Math.floor(s / 60)).padStart(2, '0')
  const ss = String(s % 60).padStart(2, '0')
  return `${m}:${ss}`
}

function scrollBottom() {
  nextTick(() => {
    if (streamRef.value) streamRef.value.scrollTop = streamRef.value.scrollHeight
  })
}

function pushMsg(m) {
  messages.value.push(m)
  scrollBottom()
}

function sys(text) {
  pushMsg({ who: 'sys', text, ts: fmt(totalSecs.value) })
}

// —— 流式呈现：按字符节奏"打字"出来，比一次性塞进去更像真人发言 ——
function streamMessage(idx, fullText, perCharMs = 35) {
  if (streamTimer) { clearInterval(streamTimer); streamTimer = null }
  let i = 0
  const m = messages.value[idx]
  if (!m) return Promise.resolve()
  m.streaming = true
  m.text = ''
  return new Promise(resolve => {
    streamTimer = setInterval(() => {
      i++
      m.text = fullText.slice(0, i)
      scrollBottom()
      if (i >= fullText.length) {
        clearInterval(streamTimer); streamTimer = null
        m.streaming = false
        m.text = fullText
        scrollBottom()
        resolve()
      }
    }, perCharMs)
  })
}

// —— 构建上报给导演的讨论历史（摘要式：只带必要字段，避免上下文膨胀）——
function buildHistory() {
  return messages.value
    .filter(m => (m.who === 'me' || m.who === 'peer' || m.who === 'moderator') &&
                 !m.live && !m.streaming && m.text)
    .map(m => ({
      who: m.who === 'me' ? 'me' : m.who === 'peer' ? 'peer' : 'sys',
      name: m.who === 'me' ? '你' : m.name,
      text: (m.text || '').slice(-300),
      phase: m.phaseKey || STAGES[m.stage]?.key || 'discussion',
      persona_id: m.personaId ?? null,
      reply_mode: m.replyMode || null,
      interrupt: !!m.interrupt,
    }))
}

// —— 思考标签轮播，营造"AI 在组织语言"的过程感 ——
const THINK_LABELS = ['正在听你刚才的发言…', '正在整理回应思路…', '正在组织观点…', '正在打字…']
let thinkTimer = null
function startThinking(name, color) {
  speakingName.value = name
  thinkingColor.value = color || 'var(--text-3)'
  aiThinking.value = true
  let i = 0
  thinkingLabel.value = THINK_LABELS[0]
  if (thinkTimer) clearInterval(thinkTimer)
  thinkTimer = setInterval(() => {
    i = (i + 1) % THINK_LABELS.length
    thinkingLabel.value = THINK_LABELS[i]
  }, 600)
}
function stopThinking() {
  aiThinking.value = false
  speakingName.value = ''
  thinkingColor.value = ''
  if (thinkTimer) { clearInterval(thinkTimer); thinkTimer = null }
}

const sleep = (ms) => new Promise(r => setTimeout(r, ms))

// —— 播放一条候选人动作（思考延时 + 逐字流式呈现）——
async function playPeerAction(a) {
  startThinking(a.name, a.color)
  const text = a.text || '我认同这个方向，先这样推进。'
  await sleep(Math.min(1800, 700 + text.length * 15))
  stopThinking()
  pushMsg({
    who: 'peer', name: a.name, style: a.style, color: a.color,
    personaId: a.persona_id,
    replyTo: a.reply_to || '', replyMode: a.reply_mode || '',
    interrupt: !!a.interrupt,
    phaseKey: a.phaseKey,
    text: '', ts: fmt(totalSecs.value), streaming: true,
  })
  const idx = messages.value.length - 1
  // 打字节奏：抢话者最快，话少的人稍慢
  const cps = a.interrupt ? 20 : 32
  await streamMessage(idx, text, cps)
  await sleep(280)
}

// —— 顺序播放导演返回的一批动作（主持人/候选人混排）——
async function playActions(actions) {
  let curIdx = stage.value   // 随"阶段推进"主持人口播前进，用于给消息打正确的 phase
  for (const a of actions) {
    if (a.type === 'moderator') {
      await sleep(450)
      if (a.subtype === 'transition' && curIdx < STAGES.length - 1) curIdx += 1
      pushMsg({
        who: 'moderator', text: a.text, subtype: a.subtype,
        phaseKey: STAGES[curIdx].key,
        ts: fmt(totalSecs.value),
      })
    } else if (a.type === 'peer') {
      await playPeerAction({ ...a, phaseKey: STAGES[curIdx].key })
    }
  }
}

// —— 调用后端导演：谁发言、回应谁、是否抢话、是否主持人控场，全由后端决定 ——
async function direct(eventType, depth = 0) {
  waitingPeer.value = true
  let res = null
  try {
    res = await sessionApi.groupDirect(props.sessionId, {
      phase: STAGES[stage.value].key,
      event: { type: eventType },
      topic: topic.value,
      persona_ids: peers.value.map(p => p.id).filter(id => id > 0),
      history: buildHistory(),
    })
  } catch (e) {
    sys('（候选人暂时连线失败，请继续你的发言，稍后会恢复回应）')
    waitingPeer.value = false
    return null
  }
  await playActions(res.actions || [])
  const idx = PHASE_IDX[res.phase]
  if (idx !== undefined) stage.value = idx
  discussionFinished.value = !!res.finished
  waitingPeer.value = false
  // 陈述阶段：你已陈述后，其余候选人逐位自动陈述，直到推进到自由交锋
  // （continue 只驱动下一位，后端推进阶段后条件不再满足，链路自然终止；depth 兜底防环）
  if (res.phase === 'statement' && myStageCount('statement') > 0 &&
      !res.finished && depth < 8) {
    await direct('continue', depth + 1)
  }
  return res
}

// —— 手动推进阶段按钮（后端轮次超限也会自动推进，按钮是主动通道）——
async function advanceStage() {
  if (recording.value) await stopRecord()
  if (waitingPeer.value) return
  await direct('advance')
}

// —— 我的发言提交后，交导演决定谁回应（各阶段统一入口）——
async function afterMySpeech() {
  await direct('user_spoke')
}

async function submitDiscussion() {
  if (recording.value) await stopRecord()
  const mine = myMessages.value
  if (!mine.length) { message.warning('请先发言再提交'); return }
  submitting.value = true
  try {
    const parts = []
    for (const s of STAGES) {
      const t = myMessages.value
        .filter(m => (m.phaseKey || STAGES[m.stage]?.key) === s.key)
        .map(m => m.text).join('\n')
      if (t) parts.push(`【${s.label}】\n${t}`)
    }
    await sessionApi.submitAnswer(props.sessionId, sqId.value, {
      transcript: parts.join('\n\n'), audio_url: '',
      duration_ms: totalSecs.value * 1000,
    })
    const res = await sessionApi.finish(props.sessionId)
    flow.setReport(res.report_id)
    message.success('群面结束，报告已生成')
    router.push(`/report/${res.report_id}`)
  } catch (e) {
    message.error('提交失败，请重试')
  } finally {
    submitting.value = false
  }
}

// —— 我的发言（语音 / 文字） ——
function toggleRecord() {
  if (waitingPeer.value) { message.info('请等待其他候选人发言完毕'); return }
  if (recording.value) stopRecord()
  else startRecord()
}

function upsertLive(text) {
  liveText = text
  const arr = messages.value
  const last = arr[arr.length - 1]
  if (last && last.who === 'me' && last.live) {
    last.text = text
    last.ts = fmt(recSecs.value)
  } else {
    arr.push({
      who: 'me', text, ts: fmt(recSecs.value), live: true,
      stage: stage.value, phaseKey: STAGES[stage.value].key,
    })
  }
  scrollBottom()
}

function finalizeLive() {
  const arr = messages.value
  const last = arr[arr.length - 1]
  if (last && last.who === 'me' && last.live) {
    last.live = false
    if (!last.text) arr.pop()
  }
  scrollBottom()
}

async function startRecord() {
  recording.value = true
  recSecs.value = 0
  committedText = ''
  liveText = ''
  recTimer = setInterval(() => {
    recSecs.value++; totalSecs.value++
    if (recSecs.value > 180) { message.info('单次发言建议不超过 3 分钟'); stopRecord() }
  }, 1000)

  if (textMode.value) return

  try {
    asrClient = createAsrClient({
      getToken: () => localStorage.getItem('mockwise_token'),
      onPartial: (t) => upsertLive(committedText + t),
      onFinal: (t) => {
        if (t) committedText += t
        upsertLive(committedText)
      },
      onError: (err) => {
        if (err?.code === 'not_configured' || err?.message === 'ASR_UNAVAILABLE') {
          fallbackToText('语音服务不可用，请改用文字发言')
        }
      },
    })
    await asrClient.start()
  } catch (e) {
    const TIPS = {
      MIC_DENIED: '麦克风权限被拒绝：请在浏览器地址栏允许麦克风后重试，或改用文字发言',
      MIC_NOT_FOUND: '未检测到麦克风设备，可改用文字发言',
      MIC_BUSY: '麦克风被占用，可改用文字发言',
      MIC_INSECURE: '请使用 http://localhost:5173 访问（IP 访问会禁用麦克风）',
    }
    fallbackToText(TIPS[e?.message] || '语音启动失败，可改用文字发言')
  }
}

function fallbackToText(tip) {
  if (asrClient) { asrClient.dispose(); asrClient = null }
  recording.value = false
  clearInterval(recTimer)
  textMode.value = true
  message.warning({ content: tip, duration: 5 })
}

async function stopRecord() {
  recording.value = false
  clearInterval(recTimer)
  if (asrClient) {
    const c = asrClient
    asrClient = null
    // 先等讯飞 final 落地（内部最多等 3s），再定型并触发候选人回应，避免重复/空文本
    try { await c.stop() } catch { /* ignore */ }
    try { c.dispose() } catch { /* ignore */ }
  }
  const myText = committedText || liveText
  finalizeLive()
  // 停止后把当前阶段作为 phase 标在消息上，并交导演决定谁回应
  if (myText) {
    const last = messages.value[messages.value.length - 1]
    if (last && last.who === 'me') last.phaseKey = STAGES[stage.value].key
    afterMySpeech()
  }
}

function sendText() {
  const t = textInput.value.trim()
  if (!t) return
  if (waitingPeer.value) { message.info('请等待其他候选人发言完毕'); return }
  pushMsg({
    who: 'me', text: t, ts: fmt(totalSecs.value),
    stage: stage.value, phaseKey: STAGES[stage.value].key,
  })
  textInput.value = ''
  afterMySpeech()
}

// —— 初始化 ——
let bootStarted = false
async function boot() {
  if (bootStarted || !props.sessionId) return
  bootStarted = true
  // 加载 AI 候选人（后端已按五型配比挑选并附带特质字段）
  try {
    const list = await sessionApi.peers(props.sessionId)
    peers.value = list || []
  } catch { /* 空列表兜底 */ }
  if (!peers.value.length) {
    peers.value = [
      { id: 1, name: '赵启明', style: '激进派', color: '#D8504F', bio: '语速快观点猛' },
      { id: 3, name: '周明远', style: '稳健派', color: '#2FA36B', bio: '先问最坏情况' },
    ]
  }
  totalTimer = setInterval(() => { if (!recording.value) totalSecs.value++ }, 1000)
  // enter 事件：主持人开场 + 破冰者发言，全部由导演决定
  await direct('enter')
}

// sessionId 可能在父组件异步创建 session 后才就绪，用 watch 兜住时序
watch(() => props.sessionId, (v) => { if (v) boot() }, { immediate: true })

onUnmounted(() => {
  clearInterval(recTimer)
  clearInterval(totalTimer)
  if (streamTimer) { clearInterval(streamTimer); streamTimer = null }
  if (thinkTimer) { clearInterval(thinkTimer); thinkTimer = null }
  if (asrClient) { asrClient.dispose(); asrClient = null }
})
</script>

<style scoped>
.topic-card { padding: 20px 24px; margin-bottom: 18px; display: flex; flex-direction: column; gap: 10px; }
.topic-head { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
.stage-tabs { display: flex; gap: 8px; flex: 1; }
.stage-tab {
  padding: 5px 14px; border-radius: 18px; font-size: 12px; font-weight: 600;
  background: var(--bg); color: var(--text-3); border: 1.5px solid transparent;
}
.stage-tab.active { background: var(--primary-soft); color: var(--primary); border-color: var(--primary); }
.stage-tab.done { color: var(--success); }
.topic-title { font-size: 17px; font-weight: 700; line-height: 1.5; }
.topic-hint { font-size: 12px; color: var(--text-3); }

.group-layout { display: grid; grid-template-columns: 300px 1fr 280px; gap: 18px; }
.card-title { margin: 0 0 12px; font-size: 14px; }

.peers-card { display: flex; flex-direction: column; }
.peer-item {
  display: flex; gap: 10px; align-items: center;
  padding: 10px; border-radius: 12px; margin-bottom: 8px;
  transition: all .2s; border: 1.5px solid transparent;
}
.peer-item.speaking { background: var(--primary-soft); border-color: var(--primary); }
.peer-item.me { background: var(--bg); }
.p-avatar {
  width: 38px; height: 38px; border-radius: 50%; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center;
  color: #fff; font-weight: 700; font-size: 14px;
}
.p-info { min-width: 0; }
.p-name { font-size: 13px; font-weight: 700; display: flex; align-items: center; gap: 6px; }
.p-style { font-size: 11px; font-weight: 600; }
.p-bio {
  font-size: 11px; color: var(--text-3);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}

.chat-card { display: flex; flex-direction: column; padding: 18px; min-height: 560px; }
.chat-stream { flex: 1; overflow-y: auto; max-height: 460px; padding-right: 6px; }
.msg-row { display: flex; gap: 10px; margin-bottom: 16px; }
.msg-row.me { flex-direction: row-reverse; }
.msg-row.sys { justify-content: center; }
.msg-row.sys .msg-meta { display: none; }
.msg-row.sys .msg-bubble {
  background: var(--warning-mid); color: var(--warning);
  font-size: 12px; padding: 8px 16px; border-radius: 16px;
}

/* —— 主持人控场气泡（居中、深色描边）—— */
.msg-row.moderator { justify-content: center; }
.mod-bubble {
  display: inline-flex; align-items: center; gap: 8px; flex-wrap: wrap;
  max-width: 92%;
  background: #2B2F36; color: #F5F6F9;
  font-size: 12px; line-height: 1.6;
  padding: 9px 18px; border-radius: 18px;
  box-shadow: 0 2px 8px rgba(0,0,0,.12);
}
.mod-icon { font-size: 14px; }
.mod-name { font-weight: 700; color: #FFD66B; }
.mod-tag {
  font-size: 10px; font-weight: 600; padding: 1px 8px; border-radius: 8px;
  background: rgba(255,255,255,.16); color: #FFD66B;
}

/* —— 抢话标记 —— */
.msg-bubble.interrupted {
  border-top: 2px dashed var(--danger);
}
.interrupt-tag {
  color: var(--danger) !important;
  border: 1px solid var(--danger);
  background: transparent !important;
}
.msg-avatar {
  width: 34px; height: 34px; border-radius: 50%; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center;
  color: #fff; font-weight: 700; font-size: 13px;
}
.msg-body { max-width: 78%; display: flex; flex-direction: column; gap: 5px; }
.msg-row.me .msg-body { align-items: flex-end; }
.msg-meta { font-size: 11px; color: var(--text-3); }
.msg-bubble {
  padding: 11px 15px; border-radius: 12px; font-size: 13px; line-height: 1.65;
  background: #F5F6F9; color: var(--text-1);
}
.msg-bubble.me { background: var(--primary); color: #fff; }
.msg-bubble.typing { display: flex; gap: 5px; padding: 14px 18px; }
.msg-bubble.typing span {
  width: 7px; height: 7px; border-radius: 50%; background: var(--text-3);
  animation: blink 1.2s infinite;
}
.msg-bubble.typing span:nth-child(2) { animation-delay: .2s; }
.msg-bubble.typing span:nth-child(3) { animation-delay: .4s; }
@keyframes blink { 0%, 60%, 100% { opacity: .3; } 30% { opacity: 1; } }

.chat-input {
  border-top: 1px solid var(--border-soft); padding-top: 14px; margin-top: 10px;
  display: flex; flex-direction: column; gap: 10px;
}
.input-btns { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.action-hint { font-size: 12px; color: var(--text-3); }
.mic-btn {
  padding: 11px 20px; border-radius: var(--r-pill);
  background: var(--primary); color: #fff; border: none;
  display: inline-flex; align-items: center; gap: 8px; font-weight: 600; cursor: pointer;
}
.mic-btn.recording { background: var(--danger); }

.my-card { display: flex; flex-direction: column; gap: 12px; }
.metric-row { display: flex; justify-content: space-between; align-items: center; }
.metric-row .n { font-size: 13px; color: var(--text-2); }
.metric-row .v { font-size: 13px; font-weight: 700; }
.bar-shell { height: 8px; background: var(--border-soft); border-radius: 4px; overflow: hidden; }
.bar-shell > span { display: block; height: 100%; background: var(--success); border-radius: 4px; transition: width .3s; }

@media (max-width: 1100px) { .group-layout { grid-template-columns: 1fr; } }

/* —— 流式打字光标 —— */
.cursor {
  display: inline-block;
  margin-left: 2px;
  color: currentColor;
  opacity: .7;
  animation: cursor-blink 1s steps(2, start) infinite;
}
@keyframes cursor-blink { to { visibility: hidden; } }

/* —— "↳ 回应 XX" 标签 —— */
.reply-to {
  display: inline-block;
  margin-left: 6px;
  padding: 1px 8px;
  font-size: 10px;
  font-weight: 600;
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.18);
  color: var(--text-3);
}

/* —— 节奏感条下方说明 —— */
.rhythm-hint {
  font-size: 11px;
  color: var(--text-3);
  margin-top: 6px;
  line-height: 1.5;
}
</style>
