<template>
  <section>
    <v-tabs v-model="activeTab" grow show-arrows>
      <v-tab>
        {{ $t("queue_next_tracks") + " (" + next_items.length + ")" }}</v-tab
      >
      <v-tab-item>
        <v-list two-line>
          <RecycleScroller
            class="scroller"
            :items="next_items"
            :item-size="72"
            key-field="queue_item_id"
            v-slot="{ item }"
            page-mode
          >
            <ListviewItem
              v-bind:item="item"
              :hideavatar="item.media_type == 3 ? $store.isMobile : false"
              :hidetracknum="true"
              :hideproviders="$store.isMobile"
              :hidelibrary="$store.isMobile"
              :hidemenu="$store.isMobile"
              :onclickHandler="itemClicked"
            ></ListviewItem>
          </RecycleScroller>
        </v-list>
      </v-tab-item>
      <v-tab>
        {{
          $t("queue_previous_tracks") + " (" + previous_items.length + ")"
        }}</v-tab
      >
      <v-tab-item>
        <v-list two-line>
          <RecycleScroller
            class="scroller"
            :items="previous_items"
            :item-size="72"
            key-field="queue_item_id"
            v-slot="{ item }"
            page-mode
          >
            <ListviewItem
              v-bind:item="item"
              :hideavatar="item.media_type == 3 ? $store.isMobile : false"
              :hidetracknum="true"
              :hideproviders="$store.isMobile"
              :hidelibrary="$store.isMobile"
              :hidemenu="$store.isMobile"
              :onclickHandler="itemClicked"
            ></ListviewItem>
          </RecycleScroller>
        </v-list>
      </v-tab-item>
      <v-menu offset-y>
        <template v-slot:activator="{ on }">
          <v-btn text v-on="on" class="align-self-center mr-4" v-if="!$store.isMobile">
            {{ $t("queue_options") }}
            <v-icon right>arrow_drop_down</v-icon>
          </v-btn>
          <v-btn icon v-on="on" class="align-self-center mr-4" v-if="$store.isMobile">
            <v-icon>settings</v-icon>
          </v-btn>
        </template>

        <v-list>
          <v-list-item
            @click="
              sendQueueCommand(
                'repeat_enabled',
                !playerQueueDetails.repeat_enabled
              )
            "
          >
            <v-list-item-icon>
              <v-icon v-text="'repeat'" />
            </v-list-item-icon>
            <v-list-item-content>
              <v-list-item-title
                v-text="
                  playerQueueDetails.repeat_enabled
                    ? $t('disable_repeat')
                    : $t('enable_repeat')
                "
              />
            </v-list-item-content>
          </v-list-item>
          <v-list-item
            @click="
              sendQueueCommand(
                'shuffle_enabled',
                !playerQueueDetails.shuffle_enabled
              )
            "
          >
            <v-list-item-icon>
              <v-icon v-text="'shuffle'" />
            </v-list-item-icon>
            <v-list-item-content>
              <v-list-item-title
                v-text="
                  playerQueueDetails.shuffle_enabled
                    ? $t('disable_shuffle')
                    : $t('enable_shuffle')
                "
              />
            </v-list-item-content>
          </v-list-item>
          <v-list-item @click="sendQueueCommand('clear')">
            <v-list-item-icon>
              <v-icon v-text="'clear'" />
            </v-list-item-icon>
            <v-list-item-content>
              <v-list-item-title v-text="$t('queue_clear')" />
            </v-list-item-content>
          </v-list-item>
        </v-list>
      </v-menu>
    </v-tabs>
    <v-dialog
      v-model="showPlayMenu"
      max-width="500px"
    >
      <v-card>
        <v-subheader class="title">{{ selectedItem.name }}</v-subheader>
        <v-list>
          <v-list-item @click="sendQueueCommand('index',selectedItem.queue_item_id)">
            <v-list-item-icon>
              <v-icon v-text="'play_circle_outline'" />
            </v-list-item-icon>
            <v-list-item-content>
              <v-list-item-title
                v-text="$t('play_now')"
              />
            </v-list-item-content>
          </v-list-item>
          <v-list-item @click="sendQueueCommand('next',selectedItem.queue_item_id)">
            <v-list-item-icon>
              <v-icon v-text="'queue_play_next'" />
            </v-list-item-icon>
            <v-list-item-content>
              <v-list-item-title
                v-text="$t('play_next')"
              />
            </v-list-item-content>
          </v-list-item>
          <v-list-item @click="sendQueueCommand('move_up',selectedItem.queue_item_id)">
            <v-list-item-icon>
              <v-icon v-text="'arrow_upward'" />
            </v-list-item-icon>
            <v-list-item-content>
              <v-list-item-title
                v-text="$t('queue_move_up')"
              />
            </v-list-item-content>
          </v-list-item>
          <v-list-item @click="sendQueueCommand('move_down',selectedItem.queue_item_id)">
            <v-list-item-icon>
              <v-icon v-text="'arrow_downward'" />
            </v-list-item-icon>
            <v-list-item-content>
              <v-list-item-title
                v-text="$t('queue_move_down')"
              />
            </v-list-item-content>
          </v-list-item>
        </v-list>
      </v-card>
    </v-dialog>
  </section>
</template>

<script>
import Vue from 'vue'
import ListviewItem from '@/components/ListviewItem.vue'

export default {
  components: {
    ListviewItem
  },
  props: {},
  data () {
    return {
      items: [],
      activeTab: 0,
      playerQueueDetails: {},
      showPlayMenu: false,
      selectedItem: {}
    }
  },
  computed: {
    next_items () {
      if (this.playerQueueDetails) {
        return this.items.slice(this.playerQueueDetails.cur_index)
      } else return []
    },
    previous_items () {
      if (this.playerQueueDetails && this.$server.activePlayer) {
        return this.items.slice(0, this.playerQueueDetails.cur_index)
      } else return []
    }
  },
  created () {
    this.$store.windowtitle = this.$t('queue')
    this.$server.$on('queue updated', this.onQueueDetailsEvent)
    this.$server.$on('queue items updated', this.onQueueItemsEvent)
    this.$server.$on('new player selected', this.activePlayerChanged)
    if (this.$server.activePlayerId) this.activePlayerChanged()
  },
  methods: {
    itemClicked (item) {
      this.selectedItem = item
      this.showPlayMenu = !this.showPlayMenu
    },
    async activePlayerChanged () {
      /// get queue details once when we have a new active player
      const endpoint = 'players/' + this.$server.activePlayerId + '/queue'
      const queueDetails = await this.$server.getData(endpoint)
      await this.onQueueDetailsEvent(queueDetails)
      await this.onQueueItemsEvent(queueDetails)
    },
    async onQueueDetailsEvent (data) {
      if (data.player_id === this.$server.activePlayerId) {
        for (const [key, value] of Object.entries(data)) {
          Vue.set(this.playerQueueDetails, key, value)
        }
      }
    },
    async onQueueItemsEvent (data) {
      if (data.player_id === this.$server.activePlayerId) {
        const endpoint = 'players/' + data.player_id + '/queue/items'
        await this.$server.getAllItems(endpoint, this.items)
      }
    },
    sendQueueCommand (cmd, cmd_args = null) {
      const endpoint = 'players/' + this.$server.activePlayerId + '/queue/' + cmd
      this.$server.putData(endpoint, cmd_args)
    }
  }
}
</script>
