var Config = Vue.component('Config', {
  template: `
    <section>

      <v-list two-line>

        <!-- base/generic config -->
        <v-list-group prepend-icon="settings" no-action>
            <template v-slot:activator>
              <v-list-tile>
                <v-list-tile-content>
                  <v-list-tile-title>{{ $t('generic_settings') }}</v-list-tile-title>
                </v-list-tile-content>
              </v-list-tile>
            </template>
            <template v-for="(conf_value, conf_key) in conf.base">
                <v-list-tile>
                  <v-list-tile-content>
                    <v-list-tile-title class="title">{{ conf_key }}</v-list-tile-title>
                  </v-list-tile-content>
                </v-list-tile>
                
                <div v-for="conf_item_key in conf.base[conf_key].__desc__">
                  <v-list-tile>
                        <v-switch v-if="typeof(conf_item_key[1]) == 'boolean'" v-model="conf.base[conf_key][conf_item_key[0]]" :label="$t(conf_item_key[2])"></v-switch>
                        <v-text-field v-else-if="conf_item_key[1] == '<password>'" v-model="conf.base[conf_key][conf_item_key[0]]" :label="$t(conf_item_key[2])" box type="password"></v-text-field>
                        <v-select v-else-if="conf_item_key[1] == '<player>'" v-model="conf.base[conf_key][conf_item_key[0]]" :label="$t(conf_item_key[2])" box type="password"></v-select>
                        <v-text-field v-else v-model="conf.base[conf_key][conf_item_key[0]]" :label="$t(conf_item_key[2])" box></v-text-field>
                  </v-list-tile>
              </div>
              <v-divider></v-divider>
            </template>
          </v-list-group>


          <!-- music providers -->
          <v-list-group prepend-icon="library_music" no-action>
              <template v-slot:activator>
                <v-list-tile>
                  <v-list-tile-content>
                    <v-list-tile-title>{{ $t('music_providers') }}</v-list-tile-title>
                  </v-list-tile-content>
                </v-list-tile>
              </template>
              <template v-for="(conf_value, conf_key) in conf.musicproviders">
                  <v-list-tile>
                    <v-list-tile-avatar>
                        <img :src="'images/icons/' + conf_key + '.png'"/>
                    </v-list-tile-avatar>
                    <v-list-tile-content>
                      <v-list-tile-title class="title">{{ conf_key }}</v-list-tile-title>
                    </v-list-tile-content>
                  </v-list-tile>
                  
                  <div v-for="conf_item_key in conf.musicproviders[conf_key].__desc__">
                    <v-list-tile>
                          <v-switch v-if="typeof(conf_item_key[1]) == 'boolean'" v-model="conf.musicproviders[conf_key][conf_item_key[0]]" :label="$t(conf_item_key[2])"></v-switch>
                          <v-text-field v-else-if="conf_item_key[1] == '<password>'" v-model="conf.musicproviders[conf_key][conf_item_key[0]]" :label="$t(conf_item_key[2])" box type="password"></v-text-field>
                          <v-select v-else-if="conf_item_key[1] == '<player>'" v-model="conf.musicproviders[conf_key][conf_item_key[0]]" :label="$t(conf_item_key[2])" box type="password"></v-select>
                          <v-text-field v-else v-model="conf.musicproviders[conf_key][conf_item_key[0]]" :label="$t(conf_item_key[2])" box></v-text-field>
                    </v-list-tile>
                </div>
                <v-divider></v-divider>
              </template>
            </v-list-group>

          <!-- player providers -->
          <v-list-group prepend-icon="speaker_group" no-action>
              <template v-slot:activator>
                <v-list-tile>
                  <v-list-tile-content>
                    <v-list-tile-title>{{ $t('player_providers') }}</v-list-tile-title>
                  </v-list-tile-content>
                </v-list-tile>
              </template>
              <template v-for="(conf_value, conf_key) in conf.playerproviders">
                  <v-list-tile>
                    <v-list-tile-avatar>
                        <img :src="'images/icons/' + conf_key + '.png'"/>
                    </v-list-tile-avatar>
                    <v-list-tile-content>
                      <v-list-tile-title class="title">{{ conf_key }}</v-list-tile-title>
                    </v-list-tile-content>
                  </v-list-tile>
                  
                  <div v-for="conf_item_key in conf.playerproviders[conf_key].__desc__">
                    <v-list-tile>
                          <v-switch v-if="typeof(conf_item_key[1]) == 'boolean'" v-model="conf.playerproviders[conf_key][conf_item_key[0]]" :label="$t(conf_item_key[2])"></v-switch>
                          <v-text-field v-else-if="conf_item_key[1] == '<password>'" v-model="conf.playerproviders[conf_key][conf_item_key[0]]" :label="$t(conf_item_key[2])" box type="password"></v-text-field>
                          <v-select v-else-if="conf_item_key[1] == '<player>'" v-model="conf.playerproviders[conf_key][conf_item_key[0]]" :label="$t(conf_item_key[2])" box type="password"></v-select>
                          <v-text-field v-else v-model="conf.playerproviders[conf_key][conf_item_key[0]]" :label="$t(conf_item_key[2])" box></v-text-field>
                    </v-list-tile>
                </div>
                <v-divider></v-divider>
              </template>
            </v-list-group>

          <!-- player settings -->
          <v-list-group prepend-icon="speaker" no-action>
              <template v-slot:activator>
                <v-list-tile>
                  <v-list-tile-content>
                    <v-list-tile-title>{{ $t('player_settings') }}</v-list-tile-title>
                  </v-list-tile-content>
                </v-list-tile>
              </template>
              <template v-for="(player, key) in players" v-if="key != '__desc__' && key in players">
                  <v-list-tile>
                    <v-list-tile-content>
                      <v-list-tile-title class="title">{{ players[key].name }}</v-list-tile-title>
                      <v-list-tile-sub-title class="title">ID: {{ key }} Provider: {{ players[key].player_provider }}</v-list-tile-sub-title>
                    </v-list-tile-content>
                  </v-list-tile>
                  
                  <div v-for="conf_item_key in conf.player_settings.__desc__" v-if="conf.player_settings[key].enabled">
                    <v-list-tile>
                          <v-switch v-if="typeof(conf_item_key[1]) == 'boolean'" v-model="conf.player_settings[key][conf_item_key[0]]" :label="$t(conf_item_key[2])"></v-switch>
                          <v-text-field v-else-if="conf_item_key[1] == '<password>'" v-model="conf.player_settings[key][conf_item_key[0]]" :label="$t(conf_item_key[2])" box type="password"></v-text-field>
                          <v-select v-else-if="conf_item_key[1] == '<player>'" v-model="conf.player_settings[key][conf_item_key[0]]" :label="$t(conf_item_key[2])" 
                            :items="playersLst"
                            item-text="name"
                            item-value="id" box>
                          </v-select>
                          <v-text-field v-else v-model="conf.player_settings[key][conf_item_key[0]]" :label="$t(conf_item_key[2])" box></v-text-field>
                    </v-list-tile>
                    <v-list-tile v-if="!conf.player_settings[key].enabled">
                          <v-switch v-model="conf.player_settings[key].enabled" :label="$t('enabled')"></v-switch>
                    </v-list-tile>
                </div>
                <div v-if="!conf.player_settings[key].enabled">
                    <v-list-tile>
                        <v-switch v-model="conf.player_settings[key].enabled" :label="$t('enabled')"></v-switch>
                    </v-list-tile>
                </div>
                <v-divider></v-divider>
              </template>
            </v-list-group>

            <v-btn @click="saveConfig()">Save</v-btn>
        </v-list>
    </section>
  `,
  props: [],
  data() {
    return {
      conf: {},
      players: {}
    }
  },
  computed: {
    playersLst()
    {
      var playersLst = [];
      for (player_id in this.conf.player_settings)
        if (player_id != '__desc__')
          playersLst.push({id: player_id, name: this.conf.player_settings[player_id].name})
      return playersLst;
    }

  },
  created() {
    this.$globals.windowtitle = this.$t('settings');
    this.getPlayers();
    this.getConfig();
    console.log(this.$globals.all_players);
  },
  methods: {
    getConfig () {
      axios
        .get('/api/config')
        .then(result => {
          this.conf = result.data;
        })
        .catch(error => {
          console.log("error", error);
        });
    },
    saveConfig () {
      axios
        .post('/api/config', this.conf)
        .then(result => {
          console.log(result);
        })
        .catch(error => {
          console.log("error", error);
        });
    },
    getPlayers () {
      const api_url = '/api/players';
      axios
        .get(api_url)
        .then(result => {
          for (var item of result.data)
            this.$set(this.players, item.player_id, item)
        })
        .catch(error => {
          console.log("error", error);
          this.showProgress = false;
        });
    },
  }
})
