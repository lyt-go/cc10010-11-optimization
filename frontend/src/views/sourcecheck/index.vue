<template>
  <section class="page" data-module="sourcecheck">
    <header class="page-head">
      <div>
        <h2>源端校验管理</h2>
        <p class="page-desc">
          复合校验按表归并成一份阻断结论快照：行数、空值、新鲜度、分区完整性取同一规则版本下的命中结果，
          执行模块只读取这份快照。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记校验配置</button>
        <button class="btn" type="button" @click="recompute">重新计算阻断结论</button>
        <button class="btn" type="button" @click="exportRows">导出源端校验清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">
            暂无源端校验数据，可先登记校验配置
          </td>
        </tr>
      </tbody>
    </table>

    <section class="panel">
      <header class="panel-head">
        <div>
          <h3>阻断结论快照</h3>
          <p class="page-desc">
            同一张表的多条规则按规则版本归并；命中规则、阻断级别与执行模块读取的是同一个结论版本。
          </p>
        </div>
        <button class="btn ghost" type="button" @click="loadSnapshots">刷新快照</button>
      </header>

      <table class="data-table">
        <thead>
          <tr>
            <th>源端数据表</th>
            <th>结论版本</th>
            <th>快照编号</th>
            <th>规则版本</th>
            <th>阻断级别</th>
            <th>命中规则</th>
            <th>运行结论</th>
            <th>说明</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="item in snapshots"
            :key="String(item.快照编号)"
            :class="['clickable-row', { 'row-active': item.源端数据表 === selectedSnapshot?.源端数据表 }]"
            @click="pickSnapshot(item.源端数据表)"
          >
            <td>{{ item.源端数据表 }}</td>
            <td>v{{ item.结论版本 }}</td>
            <td>{{ item.快照编号 }}</td>
            <td>{{ item.规则版本 }}</td>
            <td>
              <span :class="['badge', levelClass(item.阻断级别)]">{{ item.阻断级别 }}</span>
            </td>
            <td>{{ (item.命中规则 || []).join('、') || '—' }}</td>
            <td>{{ item.运行结论 }}</td>
            <td class="muted-cell">{{ item.原因 || '—' }}</td>
            <td>
              <button class="link" type="button" @click.stop="retryTable(item.源端数据表)">重算</button>
            </td>
          </tr>
          <tr v-if="!snapshots.length">
            <td colspan="9" class="empty-state">{{ snapshotEmptyReason }}</td>
          </tr>
        </tbody>
      </table>

      <div v-if="selectedSnapshot" class="detail-grid">
        <article class="detail-card">
          <h4>{{ selectedSnapshot.源端数据表 }} · 维度结论</h4>
          <ul class="kv-list">
            <li v-for="(value, name) in selectedSnapshot.维度结论" :key="name">
              <span>{{ name }}</span>
              <strong :class="levelClass(value.阻断级别)">{{ value.阻断级别 }}</strong>
              <small>{{ (value.命中规则 || []).join('、') || '未命中' }}</small>
            </li>
          </ul>
        </article>
        <article class="detail-card">
          <h4>命中依据</h4>
          <ul class="kv-list">
            <li v-for="hit in selectedSnapshot.命中规则明细" :key="hit.规则编号">
              <span>{{ hit.规则编号 }} · v{{ hit.规则版本 }}</span>
              <strong>{{ hit.阻断级别 }}</strong>
              <small>{{ hit.命中原因 }}</small>
            </li>
            <li v-if="!(selectedSnapshot.命中规则明细 || []).length" class="empty-inline">
              {{ selectedSnapshot.配置状态 === '已关闭' ? '配置已关闭，命中结果仅留档' : '当前没有命中规则' }}
            </li>
          </ul>
        </article>
        <article class="detail-card">
          <h4>规则版本冲突</h4>
          <ul class="kv-list">
            <li v-for="(item, index) in selectedSnapshot.冲突说明" :key="`${item.规则编号}-${index}`">
              <span>{{ item.规则编号 }} · {{ item.处理口径 }}</span>
              <strong>采用 v{{ item.采用规则版本 }} {{ item.采用阻断级别 }}</strong>
              <small>未采用 v{{ item.未采用规则版本 }}：{{ item.未采用命中原因 || '—' }}</small>
            </li>
            <li v-if="!(selectedSnapshot.冲突说明 || []).length" class="empty-inline">没有规则版本冲突</li>
          </ul>
        </article>
      </div>
    </section>

    <section class="panel">
      <header class="panel-head">
        <div>
          <h3>校验结果批次</h3>
          <p class="page-desc">
            同一张表的多批结果按规则版本归并；内容相同的批次重复提交只生成一次阻断结果。
          </p>
        </div>
        <button class="btn ghost" type="button" @click="loadBatches">刷新批次</button>
      </header>

      <table class="data-table">
        <thead>
          <tr>
            <th>批次编号</th>
            <th>源端数据表</th>
            <th>规则版本</th>
            <th>提交人</th>
            <th>提交时间</th>
            <th>规则条数</th>
            <th>快照编号</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in batches" :key="String(item.id)">
            <td>{{ item.批次编号 }}</td>
            <td>{{ item.源端数据表 }}</td>
            <td>v{{ item.规则版本 }}</td>
            <td>{{ item.提交人 }}</td>
            <td>{{ item.提交时间 }}</td>
            <td>{{ (item.规则结果 || []).length }}</td>
            <td>{{ item.快照编号 || '写入失败待重试' }}</td>
          </tr>
          <tr v-if="!batches.length">
            <td colspan="7" class="empty-state">{{ batchEmptyReason }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条源端校验记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

type RuleHit = {
  规则编号: string
  维度?: string
  规则版本: number
  阻断级别: string
  是否命中?: boolean
  命中原因: string
  命中依据?: Record<string, unknown>
}

type Snapshot = {
  快照编号: string
  结论版本: number
  源端数据表: string
  配置状态: string
  规则版本: number
  阻断级别: string
  是否阻断: boolean
  运行结论: string
  原因?: string
  命中规则: string[]
  命中规则明细?: RuleHit[]
  参考命中规则?: string[]
  维度结论: Record<string, { 阻断级别: string; 命中规则: string[] }>
  冲突说明: { 规则编号: string; 处理口径: string; 采用规则版本: number; 采用阻断级别: string; 未采用规则版本: number; 未采用命中原因: string }[]
  被替代命中?: RuleHit[]
}

type Batch = {
  id: number
  批次编号: string
  源端数据表: string
  规则版本: number
  提交人: string
  提交时间: string
  规则结果: RuleHit[]
  快照编号: string | null
}

const ENDPOINT = '/api/sourcecheck'
const columns = ["配置编号", "源端数据表", "行数下限", "行数上限", "空值字段列表", "数据新鲜度分钟", "分区完整性", "阻断级别"]
const actions = ["提交配置评审", "启用配置", "关闭配置"]
const filterFields = columns.slice(0, 3)

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const snapshots = ref<Snapshot[]>([])
const snapshotEmptyReason = ref('')
const batches = ref<Batch[]>([])
const batchEmptyReason = ref('')
const selectedTable = ref('')

const stats = computed(() => [
  { label: '校验配置总数', value: total.value },
  { label: '已启用配置', value: rows.value.filter((row) => row.status === '已启用').length },
  { label: '阻断结论数', value: snapshots.value.length },
  { label: '阻断中的表', value: snapshots.value.filter((item) => item.是否阻断).length },
])

const selectedSnapshot = computed(
  () => snapshots.value.find((item) => item.源端数据表 === selectedTable.value) ?? snapshots.value[0] ?? null,
)

function levelClass(level?: string) {
  if (level === '阻断') return 'level-block'
  if (level === '告警') return 'level-warn'
  if (level === '提示') return 'level-info'
  if (level === '通过') return 'level-ok'
  return 'level-unknown'
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '校验配置登记入口尚未接入审批流'
}

function pickSnapshot(table: string) {
  selectedTable.value = table
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('源端校验动作未生效，请稍后重试')
    }
    await reload()
    await loadSnapshots()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '源端校验操作失败'
  }
}

async function recompute() {
  const table = selectedSnapshot.value?.源端数据表
  if (!table) {
    errorMessage.value = '还没有可重算的源端数据表，请先提交校验结果批次'
    return
  }
  await retryTable(table)
}

async function retryTable(table: string) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/retry`, {
      method: 'POST',
      body: JSON.stringify({ values: { 源端数据表: table } }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message || payload.detail || '阻断结论重算失败')
    }
    await loadSnapshots()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '阻断结论重算失败'
  }
}

async function loadSnapshots() {
  try {
    const response = await request('/api/blocking/snapshots')
    if (!response.ok) {
      throw new Error('阻断结论读取失败')
    }
    const payload = await response.json()
    snapshots.value = payload.items ?? []
    snapshotEmptyReason.value = payload.empty_reason || '还没有表生成阻断结论'
  } catch (error) {
    snapshots.value = []
    snapshotEmptyReason.value = error instanceof Error ? error.message : '阻断结论读取失败'
  }
}

async function loadBatches() {
  try {
    const response = await request(`${ENDPOINT}/batches`)
    if (!response.ok) {
      throw new Error('校验结果批次读取失败')
    }
    const payload = await response.json()
    batches.value = payload.items ?? []
    batchEmptyReason.value = payload.empty_reason || '还没有提交过校验结果批次'
  } catch (error) {
    batches.value = []
    batchEmptyReason.value = error instanceof Error ? error.message : '校验结果批次读取失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('校验配置列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '源端校验列表读取失败'
  }
}

onMounted(async () => {
  await reload()
  await Promise.all([loadSnapshots(), loadBatches()])
})
</script>

<style scoped>
.panel { margin-top: 18px; }
.panel-head { display: flex; justify-content: space-between; align-items: flex-start; margin: 0 0 8px; }
.panel-head h3 { font-size: 15px; margin: 0; }
.badge { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 12px; }
.level-block { color: #b42318; background: #fee4e2; }
.level-warn { color: #b54708; background: #fef0c7; }
.level-info { color: #175cd3; background: #d1e9ff; }
.level-ok { color: #067647; background: #d1fadf; }
.level-unknown { color: #475467; background: #f2f4f7; }
.muted-cell { color: #64748b; }
.detail-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; margin-top: 12px; }
.detail-card { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; }
.detail-card h4 { margin: 0 0 8px; font-size: 13px; }
.kv-list { list-style: none; margin: 0; padding: 0; font-size: 12px; }
.kv-list li { display: flex; flex-wrap: wrap; gap: 6px; justify-content: space-between; padding: 4px 0; border-bottom: 1px dashed #e5e7eb; }
.kv-list small { color: var(--muted); width: 100%; }
.empty-inline { color: var(--muted); justify-content: flex-start; }
.clickable-row { cursor: pointer; }
.row-active { background: #eff6ff; }
</style>
