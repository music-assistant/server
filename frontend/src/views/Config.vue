<template>
  <section>
    <v-alert :value="restart_message" type="info">
      {{ $t("reboot_required") }}
    </v-alert>

        <!-- config main menu -->
        <v-card flat v-if="!configKey">
          <v-list tile>
            <v-list-item tile
              v-for="(conf_value, conf_key) in conf" :key="conf_key" @click="$router.push('/config/' + conf_key)">
                <!-- <v-list-item-icon style="margin-left:15px">
                  <v-icon>{{ item.icon }}</v-icon>
                </v-list-item-icon> -->
                <v-list-item-content>
                  <v-list-item-title> {{ $t("conf." + conf_key) }}</v-list-item-title>
                </v-list-item-content>
            </v-list-item>
          </v-list>
        </v-card>
        <!-- generic and module settings -->
        <v-card flat v-if="configKey != 'player_settings'">
          <v-list two-line tile>
            <v-list-group
              no-action
              v-for="(conf_subvalue, conf_subkey) in conf[configKey]"
              :key="conf_subkey"
            >
              <template v-slot:activator>
                <v-list-item>
                  <v-list-item-avatar tile style="margin-left:-15px">
                    <img :src="require('../assets/' + conf_subkey + '.png')" style="border-radius:5px;border: 1px solid rgba(0,0,0,.85);" />
                  </v-list-item-avatar>
                  <v-list-item-content>
                    <v-list-item-title>{{
                      $t("conf." + conf_subkey)
                    }}</v-list-item-title>
                  </v-list-item-content>
                </v-list-item>
              </template>
              <div
                v-for="(conf_item_value, conf_item_key) in conf[configKey][
                  conf_subkey
                ].__desc__"
                :key="conf_item_key"
              >
                <v-list-item>
                  <v-switch
                    v-if="typeof conf_item_value[1] == 'boolean'"
                    v-model="conf[configKey][conf_subkey][conf_item_value[0]]"
                    :label="$t('conf.' + conf_item_value[2])"
                    @change="
                      confChanged(
                        configKey,
                        conf_subkey,
                        conf[configKey][conf_subkey]
                      )
                    "
                  ></v-switch>
                  <v-text-field
                    v-else-if="conf_item_value[1] == '<password>'"
                    v-model="conf[configKey][conf_subkey][conf_item_value[0]]"
                    :label="$t('conf.' + conf_item_value[2])"
                    filled
                    type="password"
                    @change="
                      confChanged(
                        configKey,
                        conf_subkey,
                        conf[configKey][conf_subkey]
                      )
                    "
                  ></v-text-field>
                  <v-select
                    v-else-if="conf_item_value[1] == '<player>'"
                    v-model="conf[configKey][conf_subkey][conf_item_value[0]]"
                    :label="$t('conf.' + conf_item_value[2])"
                    filled
                    type="password"
                    @change="
                      confChanged(
                        configKey,
                        conf_subkey,
                        conf[configKey][conf_subkey]
                      )
                    "
                  ></v-select>
                  <v-text-field
                    v-else
                    v-model="conf[configKey][conf_subkey][conf_item_value[0]]"
                    :label="$t('conf.' + conf_item_value[2])"
                    @change="
                      confChanged(
                        configKey,
                        conf_subkey,
                        conf[configKey][conf_subkey]
                      )
                    "
                    filled
                  ></v-text-field>
                </v-list-item>
              </div>
              <v-divider></v-divider>
            </v-list-group>
          </v-list>
        </v-card>
        <!-- player settings -->
        <v-card flat v-if="configKey == 'player_settings'">
          <v-list two-line>
            <v-list-group
              no-action
              v-for="(player, key) in $server.players"
              :key="key"
            >
              <template v-slot:activator>
                <v-list-item>
                  <v-list-item-avatar tile style="margin-left:-20px;margin-right:6px;">
                    <img
                      :src="
                        require('../assets/' + player.player_provider + '.png')
                      "
                      style="border-radius:5px;border: 1px solid rgba(0,0,0,.85);"
                    />
                  </v-list-item-avatar>
                  <v-list-item-content>
                    <v-list-item-title class="title">{{
                      player.name
                    }}</v-list-item-title>
                    <v-list-item-subtitle class="caption">{{
                      key
                    }}</v-list-item-subtitle>
                  </v-list-item-content>
                </v-list-item>
              </template>
              <div v-if="conf.player_settings[key].enabled">
                <!-- enabled player -->
                <div
                  v-for="(conf_item_value, conf_item_key) in conf
                    .player_settings[key].__desc__"
                  :key="conf_item_key"
                >
                  <v-list-item>
                    <v-switch
                      v-if="typeof conf_item_value[1] == 'boolean'"
                      v-model="conf.player_settings[key][conf_item_value[0]]"
                      :label="$t('conf.' + conf_item_value[2])"
                      @change="
                        confChanged(
                          'player_settings',
                          key,
                          conf.player_settings[key]
                        )
                      "
                    ></v-switch>
                    <v-text-field
                      v-else-if="conf_item_value[1] == '<password>'"
                      v-model="conf.player_settings[key][conf_item_value[0]]"
                      :label="$t('conf.' + conf_item_value[2])"
                      filled
                      type="password"
                      @change="
                        confChanged(
                          'player_settings',
                          key,
                          conf.player_settings[key]
                        )
                      "
                    ></v-text-field>
                    <v-select
                      v-else-if="conf_item_value[1] == '<player>'"
                      v-model="conf.player_settings[key][conf_item_value[0]]"
                      :label="$t('conf.' + conf_item_value[2])"
                      @change="
                        confChanged(
                          'player_settings',
                          key,
                          conf.player_settings[key]
                        )
                      "
                      filled
                    >
                      <option
                        v-for="(player, key) in $server.players"
                        :value="item.id"
                        :key="key"
                        >{{ item.name }}</option
                      >
                    </v-select>
                    <v-select
                      v-else-if="conf_item_value[0] == 'max_sample_rate'"
                      v-model="conf.player_settings[key][conf_item_value[0]]"
                      :label="$t('conf.' + conf_item_value[2])"
                      :items="sample_rates"
                      @change="
                        confChanged(
                          'player_settings',
                          key,
                          conf.player_settings[key]
                        )
                      "
                      filled
                    ></v-select>
                    <v-slider
                      v-else-if="conf_item_value[0] == 'crossfade_duration'"
                      v-model="conf.player_settings[key][conf_item_value[0]]"
                      :label="$t('conf.' + conf_item_value[2])"
                      @change="
                        confChanged(
                          'player_settings',
                          key,
                          conf.player_settings[key]
                        )
                      "
                      min="0"
                      max="10"
                      filled
                      thumb-label
                    ></v-slider>
                    <v-text-field
                      v-else
                      v-model="conf.player_settings[key][conf_item_value[0]]"
                      :label="$t('conf.' + conf_item_value[2])"
                      @change="
                        confChanged(
                          'player_settings',
                          key,
                          conf.player_settings[key]
                        )
                      "
                      filled
                    ></v-text-field>
                  </v-list-item>
                  <v-list-item v-if="!conf.player_settings[key].enabled">
                    <v-switch
                      v-model="conf.player_settings[key].enabled"
                      :label="$t('conf.' + 'enabled')"
                      @change="
                        confChanged(
                          'player_settings',
                          key,
                          conf.player_settings[key]
                        )
                      "
                    ></v-switch>
                  </v-list-item>
                </div>
              </div>
              <div v-else>
                <!-- disabled player -->
                <v-list-item>
                  <v-switch
                    v-model="conf.player_settings[key].enabled"
                    :label="$t('conf.' + 'enabled')"
                    @change="
                      confChanged(
                        'player_settings',
                        key,
                        conf.player_settings[key]
                      )
                    "
                  ></v-switch>
                </v-list-item>
              </div>
              <v-divider></v-divider>
            </v-list-group>
          </v-list>
        </v-card>

  </section>
</template>

<script>

export default {
  components: {
  },
  props: ['configKey'],
  data () {
    return {
      conf: {},
      players: {},
      active: 0,
      sample_rates: [44100, 48000, 88200, 96000, 192000, 384000],
      restart_message: false
    }
  },
  created () {
    this.$store.windowtitle = this.$t('settings')
    if (this.configKey) {
      this.$store.windowtitle += ' | ' + this.$t('conf.' + this.configKey)
    }
    this.getConfig()
  },
  methods: {
    async getConfig () {
      this.conf = await this.$server.getData('config')
    },
    async confChanged (key, subkey, newvalue) {
      const endpoint = 'config/' + key + '/' + subkey
      const result = await this.$server.putData(endpoint, newvalue)
      if (result.restart_required) {
        this.restart_message = true
      }
    }
  }
}
</script>
