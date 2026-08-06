<template>
  <div>
    <div class="table-actions">
      <button class="btn btn-primary" @click="openCreate()">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>
        添加阿里云账户
      </button>
    </div>
    <div v-if="error" class="err-banner">{{ error }}</div>
    <div v-if="loading" class="muted" style="padding:24px 0">加载中…</div>
    <table v-else class="tbl">
      <thead>
        <tr>
          <th>Account ID</th><th>描述</th><th>AccessKey</th><th>状态</th><th>探活</th><th></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="a in accounts" :key="a.account_id">
          <td class="cell-name mono">{{ a.account_id }}</td>
          <td>{{ a.description }}</td>
          <td class="mono" style="font-size:11.5px">{{ a.access_key_masked }}</td>
          <td>
            <span class="perm-chip" :class="a.enabled ? '' : 'danger'">{{ a.enabled ? '启用' : '禁用' }}</span>
          </td>
          <td>
            <span v-if="a.probe_error" class="perm-chip danger" :title="a.probe_error">失败</span>
            <span v-else class="perm-chip">正常</span>
          </td>
          <td>
            <div class="row-actions">
              <button class="mini-btn" @click="openEdit(a)">编辑</button>
              <button class="mini-btn danger" @click="doDelete(a)">删除</button>
            </div>
          </td>
        </tr>
        <tr v-if="!accounts.length && !loading">
          <td colspan="6" style="text-align:center;color:var(--muted);padding:24px 0">暂无账户 · 点击"添加阿里云账户"创建第一个</td>
        </tr>
      </tbody>
    </table>

    <Modal :show="!!modal" title="阿里云账户" @close="modal = null">
      <div class="field"><label>Account ID</label><input v-model="modal.account_id" :disabled="!!modal.original" placeholder="prod-main（小写字母/数字/连字符）" /></div>
      <div class="field"><label>描述</label><input v-model="modal.description" placeholder="生产主账户" /></div>
      <div class="field"><label>AccessKey ID</label><input v-model="modal.access_key_id" placeholder="LTAI..." /></div>
      <div class="field"><label>AccessKey Secret</label>
        <input v-model="modal.access_key_secret" type="password" placeholder="编辑时留空保持不变" /></div>
      <div class="field"><label>Region</label><input v-model="modal.region" placeholder="cn-hangzhou" /></div>
      <label class="perm-toggle read" style="margin:6px 0">
        <input type="checkbox" v-model="modal.enabled" />
        <span class="switch"></span><span class="plabel">启用</span>
      </label>
      <div v-if="modal.probe_error" class="err-banner" style="margin-top:8px">探活失败：{{ modal.probe_error }}（已保存，可修复凭证后重试）</div>
      <template #footer>
        <button class="btn btn-ghost" @click="modal = null">取消</button>
        <button class="btn btn-primary" :disabled="saving" @click="save">{{ saving ? '保存中…' : '保存' }}</button>
      </template>
    </Modal>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { getAliyunAccounts, createAliyunAccount, updateAliyunAccount, deleteAliyunAccount } from '../api/index.js'
import Modal from '../components/Modal.vue'

const accounts = ref([])
const loading = ref(false)
const error = ref('')
const saving = ref(false)
const modal = ref(null)

async function load() {
  loading.value = true; error.value = ''
  try { accounts.value = await getAliyunAccounts() }
  catch (e) { error.value = '加载失败: ' + e.message }
  finally { loading.value = false }
}

function openCreate() {
  modal.value = { account_id: '', description: '', access_key_id: '', access_key_secret: '',
                  region: 'cn-hangzhou', enabled: true, original: null, probe_error: null }
}
function openEdit(a) {
  modal.value = { ...a, access_key_id: '', access_key_secret: '', original: a.account_id }
}

async function save() {
  const m = modal.value
  if (!m.account_id) { error.value = 'Account ID 必填'; return }
  if (!m.original && (!m.access_key_id || !m.access_key_secret)) { error.value = '新增时 AccessKey ID/Secret 必填'; return }
  saving.value = true; error.value = ''
  try {
    if (m.original) {
      const body = { description: m.description, region: m.region, enabled: m.enabled }
      if (m.access_key_id) body.access_key_id = m.access_key_id
      if (m.access_key_secret) body.access_key_secret = m.access_key_secret
      const res = await updateAliyunAccount(m.original, body)
      m.probe_error = res.probe_error
    } else {
      const res = await createAliyunAccount({
        account_id: m.account_id, description: m.description,
        access_key_id: m.access_key_id, access_key_secret: m.access_key_secret,
        region: m.region, enabled: m.enabled,
      })
      m.probe_error = res.probe_error
    }
    if (m.probe_error) { error.value = `已保存，但探活失败：${m.probe_error}` }
    else { modal.value = null }
    await load()
  } catch (e) { error.value = '保存失败: ' + e.message }
  finally { saving.value = false }
}

async function doDelete(a) {
  if (!confirm(`确定删除账户 "${a.account_id}"？此操作不可撤销。`)) return
  try { await deleteAliyunAccount(a.account_id); await load() }
  catch (e) { error.value = '删除失败: ' + e.message }
}

onMounted(load)
</script>
