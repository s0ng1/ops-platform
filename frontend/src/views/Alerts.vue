<script setup>
// 告警中心：事件 / 规则 / 规则模板 / 通知渠道 / 静默窗口 / 日志事件 / 日志规则 七个 tab
import { ref } from 'vue'
import AlertEvents from './alerts/AlertEvents.vue'
import AlertRules from './alerts/AlertRules.vue'
import AlertTemplates from './alerts/AlertTemplates.vue'
import NotifyConfigs from './alerts/NotifyConfigs.vue'
import AlertSilences from './alerts/AlertSilences.vue'
import LogEvents from './alerts/LogEvents.vue'
import LogRules from './alerts/LogRules.vue'

const tab = ref('events')
</script>

<template>
  <el-card shadow="never">
    <el-tabs v-model="tab">
      <el-tab-pane label="告警事件" name="events">
        <AlertEvents v-if="tab === 'events'" />
      </el-tab-pane>
      <el-tab-pane label="告警规则" name="rules">
        <AlertRules v-if="tab === 'rules'" />
      </el-tab-pane>
      <el-tab-pane label="规则模板" name="templates">
        <AlertTemplates v-if="tab === 'templates'" />
      </el-tab-pane>
      <el-tab-pane label="通知渠道" name="notify">
        <NotifyConfigs v-if="tab === 'notify'" />
      </el-tab-pane>
      <el-tab-pane label="静默窗口" name="silences">
        <AlertSilences v-if="tab === 'silences'" />
      </el-tab-pane>
      <el-tab-pane label="日志事件" name="logEvents">
        <LogEvents v-if="tab === 'logEvents'" />
      </el-tab-pane>
      <el-tab-pane label="日志规则" name="logRules">
        <LogRules v-if="tab === 'logRules'" />
      </el-tab-pane>
    </el-tabs>
  </el-card>
</template>
