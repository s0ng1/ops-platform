<script setup>
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { User, Lock } from '@element-plus/icons-vue'
import { login } from '../api'
import { useAuth } from '../stores/auth'

const router = useRouter()
const { setLogin } = useAuth()

const formRef = ref()
const loading = ref(false)
const form = reactive({ username: '', password: '' })

const rules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
}

// 提交登录
async function submit() {
  await formRef.value.validate()
  loading.value = true
  try {
    const data = await login(form)
    setLogin(data)
    ElMessage.success('登录成功')
    router.push('/')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <!-- 品牌光晕背景 -->
    <div class="glow glow-a" />
    <div class="glow glow-b" />

    <div class="login-card">
      <div class="brand">
        <div class="brand-mark">
          <el-icon :size="22"><Monitor /></el-icon>
        </div>
        <div class="brand-name">内网运维管理平台</div>
        <div class="brand-sub">网络监控 · 告警 · 拓扑 · 资产管理</div>
      </div>
      <el-form ref="formRef" :model="form" :rules="rules" size="large" @keyup.enter="submit">
        <el-form-item prop="username">
          <el-input v-model="form.username" placeholder="用户名" :prefix-icon="User" />
        </el-form-item>
        <el-form-item prop="password">
          <el-input v-model="form.password" type="password" placeholder="密码" show-password :prefix-icon="Lock" />
        </el-form-item>
        <el-form-item class="submit-item">
          <el-button type="primary" class="submit-btn" :loading="loading" @click="submit">
            登 录
          </el-button>
        </el-form-item>
      </el-form>
      <div class="tip">默认账号：admin / admin123</div>
    </div>
  </div>
</template>

<style scoped>
.login-page {
  position: relative;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--op-bg-page);
  overflow: hidden;
}

/* 低透明度品牌色径向光晕 */
.glow {
  position: absolute;
  border-radius: 50%;
  pointer-events: none;
}

.glow-a {
  width: 640px;
  height: 640px;
  top: -240px;
  left: -160px;
  background: radial-gradient(circle, rgba(79, 91, 213, 0.14) 0%, rgba(79, 91, 213, 0) 70%);
}

.glow-b {
  width: 720px;
  height: 720px;
  bottom: -300px;
  right: -200px;
  background: radial-gradient(circle, rgba(79, 91, 213, 0.1) 0%, rgba(79, 91, 213, 0) 70%);
}

.login-card {
  position: relative;
  width: 400px;
  padding: 40px 36px 28px;
  background: var(--op-bg-card);
  border: 1px solid var(--op-border-light);
  border-radius: var(--op-radius-xl);
  box-shadow: var(--op-shadow-pop);
}

.brand {
  text-align: center;
  margin-bottom: 28px;
}

.brand-mark {
  width: 48px;
  height: 48px;
  margin: 0 auto 14px;
  border-radius: 12px;
  background: var(--op-color-primary);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  box-shadow: 0 6px 16px rgba(79, 91, 213, 0.3);
}

.brand-name {
  font-size: 20px;
  font-weight: 600;
  letter-spacing: 1px;
}

.brand-sub {
  margin-top: 6px;
  font-size: 13px;
  color: var(--op-text-tertiary);
  letter-spacing: 0.5px;
}

.submit-item {
  margin-bottom: 12px;
}

.submit-btn {
  width: 100%;
  font-weight: 600;
  letter-spacing: 4px;
}

.tip {
  text-align: center;
  color: var(--op-text-tertiary);
  font-size: 12px;
}
</style>
