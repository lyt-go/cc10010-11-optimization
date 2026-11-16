<template>
  <section class="page" data-module="runtime">
    <header class="page-head">
      <div>
        <h2>运行实例管理</h2>
        <p class="page-desc">
          记录每次ETL作业的运行实例；阻断结论只读源端校验产出的数据快照，运行结论跟随同一规则版本。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记运行记录</button>
        <button class="btn" type="button" @click="exportRows">导出运行实例清单</button>
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
          <td :colspan="columns.length + 1" class="empty-state">暂无运行实例数据，可先登记运行记录</td>
        </tr>
      </tbody>
    </table>

    <section class="panel">
      <header class="panel-head">
        <div>
          <h3>执行模块读取的阻断结论</h3>
          <p class="page-desc">
            快照编号、结论版本、规则版本与命中原因同源；缺失结论会说明原因，不会显示为通过。
          </p>
        </div>
        <button class="btn ghost" type="button" @click="loadBlocking">刷新结论</button>
      </header>

      <table class="data-table">
        <thead>
          <tr>
            <th>记录编号</th>
            <th>源端数据表</th>
            <th>结论版本</th>
            <th>快照编号</th>
            <th>规则版本</th>
            <th>阻断级别</th>
            <th>命中规则</th>
            <th>阻断结论</th>
            <th>原因</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in blockingRows" :key="String(item.id)">
            <td>{{ item.记录编号 }}</td>
            <td>{{ item.源端数据表 || '—' }}</td>
            <td>{{ item.阻断结论版本 ? `v${item.阻断结论版本}` : '—' }}</td>
            <td>{{ item.阻断快照编号 || '—' }}</td>
            <td>{{ item.规则版本 ?? '—' }}</td>
            <td>
              <span :class="['badge', levelClass(item.阻断级别)]">{{ item.阻断级别 || '未校验' }}</span>
            </td>
            <td>{{ item.命中规则 || '—' }}</td>
            <td>{{ item.阻断结论 || '无结论' }}</td>
            <td class="muted-cell">{{ item.阻断原因 || '—' }}</td>
            <td class="row-actions">
              <button class="link" type="button" @click="applyBlocking(item)">应用结论</button>
              <button class="link" type="button" @click="retryBlocking(item)">重试校验</button>
            </td>
          </tr>
          <tr v-if="!blockingRows.length">
            <td colspan="10" class="empty-state">{{ blockingEmptyReason }}</td>
          </tr>
        </tbody>
      </table>

      <article v-if="selectedBlocking" class="detail-card">
        <h4>{{ selectedBlocking.记录编号 }} · {{ selectedBlocking.源端数据表 }}</h4>
        <p class="muted-cell">{{ selectedBlocking.命中原因 || selectedBlocking.阻断原因 || '没有命中原因' }}</p>
        <p v-if="selectedBlocking.阻断快照编号" class="muted-cell">
          已应用快照 {{ selectedBlocking.阻断快照编号 }}，结论版本 v{{ selectedBlocking.阻断结论版本 }}，
          规则版本 v{{ selectedBlocking.规则版本 }}。
        </p>
      </article>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条运行实例记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

type BlockingRow = {
  id: number
  记录编号: string
  源端数据表: string
  阻断快照编号: string | null
  阻断结论版本: number | null
  规则版本: number | null
  阻断级别: string
  命中规则: string
  命中原因: string
  阻断结论: string
  阻断原因: string
  结论来源: string
  重试次数: number
}

const ENDPOINT = '/api/runtime'
const columns = ["记录编号", "作业名称", "开始时间", "结束时间", "执行状态", "读取行数", "写入行数", "错误信息", "阻断级别", "阻断结论"]
const actions = ["启动作业", "终止作业", "标记成功"]
const filterFields = columns.slice(0, 3)

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const blockingRows = ref<BlockingRow[]>([])
const blockingEmptyReason = ref('')
const selectedId = ref<number | null>(null)

const stats = computed(() => [
  { label: '运行记录总数', value: total.value },
  { label: '阻断中记录', value: blockingRows.value.filter((item) => item.阻断级别 === '阻断').length },
  { label: '无结论记录', value: blockingRows.value.filter((item) => !item.阻断结论版本).length },
])

const selectedBlocking = computed(
  () => blockingRows.value.find((item) => item.id === selectedId.value) ?? null,
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
  errorMessage.value = '运行记录登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    const payload = await response.json()
    if (!response.ok || payload.ok === false) {
      throw new Error(payload.message || payload.detail || '运行实例动作未生效，请稍后重试')
    }
    await reload()
    await loadBlocking()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '运行实例操作失败'
  }
}

async function postBlockingAction(id: number, action: string) {
  const response = await request(`${ENDPOINT}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  const payload = await response.json()
  if (!response.ok || payload.ok === false) {
    throw new Error(payload.message || payload.detail || '阻断结论操作未生效')
  }
  return payload.message as string
}

async function applyBlocking(item: BlockingRow) {
  errorMessage.value = ''
  selectedId.value = item.id
  try {
    await postBlockingAction(item.id, '应用阻断结论')
    await Promise.all([reload(), loadBlocking()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '应用阻断结论失败'
  }
}

async function retryBlocking(item: BlockingRow) {
  errorMessage.value = ''
  selectedId.value = item.id
  try {
    await postBlockingAction(item.id, '重试阻断校验')
    await Promise.all([reload(), loadBlocking()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重试阻断校验失败'
  }
}

async function loadBlocking() {
  try {
    const response = await request(`${ENDPOINT}/blocking`)
    if (!response.ok) {
      throw new Error('阻断结论读取失败')
    }
    const payload = await response.json()
    blockingRows.value = payload.items ?? []
    blockingEmptyReason.value = payload.empty_reason || '还没有运行记录，无法读取阻断结论'
  } catch (error) {
    blockingRows.value = []
    blockingEmptyReason.value = error instanceof Error ? error.message : '阻断结论读取失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('运行记录列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '运行实例列表读取失败'
  }
}

onMounted(async () => {
  await reload()
  await loadBlocking()
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
.detail-card { margin-top: 12px; background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; }
.detail-card h4 { margin: 0 0 6px; font-size: 13px; }
</style>
