var Config = Vue.component('Config', {
  template: `
    <section>

        <v-tabs v-model="active" color="transparent" light slider-color="black">
            <v-tab ripple v-for="(conf_value, conf_key) in conf" :key="conf_key">{{ $t('conf.'+conf_key) }}</v-tab>
                  <v-tab-item v-for="(conf_value, conf_key) in conf" :key="conf_key">

                      <!-- generic and module settings -->
                      <v-list two-line v-if="conf_key != 'player_settings'">
                          <v-list-group no-action v-for="(conf_subvalue, conf_subkey) in conf[conf_key]" :key="conf_key+conf_subkey">
                            <template v-slot:activator>
                                <v-list-tile>
                                  <v-list-tile-avatar>
                                      <img :src="'images/icons/' + conf_subkey + '.png'"/>
                                  </v-list-tile-avatar>
                                  <v-list-tile-content>
                                      <v-list-tile-title>{{ $t('conf.'+conf_subkey) }}</v-list-tile-title>
                                  </v-list-tile-content>
                                </v-list-tile>
                              </template>
                              <div v-for="conf_item_key in conf[conf_key][conf_subkey].__desc__">
                                    <v-list-tile>
                                          <v-switch v-if="typeof(conf_item_key[1]) == 'boolean'" v-model="conf[conf_key][conf_subkey][conf_item_key[0]]" :label="$t('conf.'+conf_item_key[2])" @change="confChanged(conf_key, conf_subkey, conf[conf_key][conf_subkey])"></v-switch>
                                          <v-text-field v-else-if="conf_item_key[1] == '<password>'" v-model="conf[conf_key][conf_subkey][conf_item_key[0]]" :label="$t('conf.'+conf_item_key[2])" box type="password" @change="confChanged(conf_key, conf_subkey, conf[conf_key][conf_subkey])"></v-text-field>
                                          <v-select v-else-if="conf_item_key[1] == '<player>'" v-model="conf[conf_key][conf_subkey][conf_item_key[0]]" :label="$t('conf.'+conf_item_key[2])" box type="password" @change="confChanged(conf_key, conf_subkey, conf[conf_key][conf_subkey])"></v-select>
                                          <v-text-field v-else v-model="conf[conf_key][conf_subkey][conf_item_key[0]]" :label="$t('conf.'+conf_item_key[2])" @change="confChanged(conf_key, conf_subkey, conf[conf_key][conf_subkey])" box></v-text-field>
                                    </v-list-tile>
                                </div>
                                <v-divider></v-divider>
                            </v-list-group>
                      </v-list two-line>

                      <!-- player settings -->
                      <v-list two-line v-if="conf_key == 'player_settings'">
                          <v-list-group no-action v-for="(player, key) in players" v-if="key != '__desc__' && key in players" :key="key">
                                <template v-slot:activator>
                                    <v-list-tile>
                                      <v-list-tile-avatar>
                                          <img :src="'images/icons/' + players[key].player_provider + '.png'"/>
                                      </v-list-tile-avatar>
                                      <v-list-tile-content>
                                        <v-list-tile-title class="title">{{ players[key].name }}</v-list-tile-title>
                                        <v-list-tile-sub-title class="title">{{ key }}</v-list-tile-sub-title>
                                      </v-list-tile-content>
                                  </v-list-tile>
                              </template>
                              <div v-for="conf_item_key in conf.player_settings[key].__desc__" v-if="conf.player_settings[key].enabled">
                                  <v-list-tile>
                                        <v-switch v-if="typeof(conf_item_key[1]) == 'boolean'" v-model="conf.player_settings[key][conf_item_key[0]]" :label="$t('conf.'+conf_item_key[2])" @change="confChanged('player_settings', key, conf.player_settings[key])"></v-switch>
                                        <v-text-field v-else-if="conf_item_key[1] == '<password>'" v-model="conf.player_settings[key][conf_item_key[0]]" :label="$t('conf.'+conf_item_key[2])" box type="password" @change="confChanged('player_settings', key, conf.player_settings[key])"></v-text-field>
                                        <v-select v-else-if="conf_item_key[1] == '<player>'" v-model="conf.player_settings[key][conf_item_key[0]]" :label="$t('conf.'+conf_item_key[2])" @change="confChanged('player_settings', key, conf.player_settings[key])"
                                          :items="playersLst"
                                          item-text="name"
                                          item-value="id" box>
                                        </v-select>
                                        <v-select v-else-if="conf_item_key[0] == 'max_sample_rate'" v-model="conf.player_settings[key][conf_item_key[0]]" :label="$t('conf.'+conf_item_key[2])" :items="sample_rates" @change="confChanged('player_settings', key, conf.player_settings[key])" box></v-select>
                                        <v-slider v-else-if="conf_item_key[0] == 'crossfade_duration'" v-model="conf.player_settings[key][conf_item_key[0]]" :label="$t('conf.'+conf_item_key[2])" @change="confChanged('player_settings', key, conf.player_settings[key])" min=0 max=10 box thumb-label></v-slider>
                                        <v-text-field v-else v-model="conf.player_settings[key][conf_item_key[0]]" :label="$t('conf.'+conf_item_key[2])" @change="confChanged('player_settings', key, conf.player_settings[key])" box></v-text-field>
                                  </v-list-tile>
                                  <v-list-tile v-if="!conf.player_settings[key].enabled">
                                        <v-switch v-model="conf.player_settings[key].enabled" :label="$t('conf.'+'enabled')" @change="confChanged('player_settings', key, conf.player_settings[key])"></v-switch>
                                  </v-list-tile>
                              </div>
                              <div v-if="!conf.player_settings[key].enabled">
                                  <v-list-tile>
                                      <v-switch v-model="conf.player_settings[key].enabled" :label="$t('conf.'+'enabled')" @change="confChanged('player_settings', key, conf.player_settings[key])"></v-switch>
                                  </v-list-tile>
                              </div>
                                <v-divider></v-divider>
                            </v-list-group>
                      </v-list two-line>
                  </v-tab-item>
            </v-tab>
        </v-tabs>


    </section>
  `,
  props: [],
  data() {
    return {
      conf: {},
      players: {},
      active: 0,
      sample_rates: [44100, 48000, 88200, 96000, 192000, 384000]
    }
  },
  computed: {
    playersLst()
    {
      var playersLst = [];
      playersLst.push({id: null, name: this.$t('conf.'+'not_grouped')})
      for (player_id in this.players)
        playersLst.push({id: player_id, name: this.conf.player_settings[player_id].name})
      return playersLst;
    }
  },
  watch: {},
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
    confChanged(key, subkey, newvalue) {
      console.log(key + "/" + subkey + " changed!");
      console.log(newvalue);
      axios
        .post('/api/config/'+key+'/'+subkey, newvalue)
        .then(result => {
          console.log(result);
          if (result.data.restart_required)
            this.$toasted.show(this.$t('conf.conf_saved'));
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
