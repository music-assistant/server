"""GraphQL query documents for the VRT MAX catalogue API."""

from __future__ import annotations

# Shared tile selection (fields common to every ITile, plus the episode-only
# formattedDuration on the concrete episode types).
_TILE_FIELDS = """
      __typename
      ... on ITile {
        objectId
        title
        description
        image { templateUrl }
        primaryMeta { value }
        progress { progressInSeconds durationInSeconds completed }
        action {
          __typename
          ... on LinkAction { link }
        }
      }
      ... on RadioEpisodeTile { formattedDuration }
      ... on PodcastEpisodeTile { formattedDuration }
      ... on SongTile { startDate }
"""

_QUERY_SEARCH = """
query SearchTileList($listId: ID!, $first: Int!) {
  list(listId: $listId) {
    __typename
    ... on PaginatedTileList {
      paginatedItems(first: $first) {
        edges {
          node {
TILE_FIELDS
          }
        }
      }
    }
  }
}
""".replace("TILE_FIELDS", _TILE_FIELDS)

_QUERY_LANDING = """
query ThemePage($pageId: ID!) {
  page(id: $pageId) {
    __typename
    ... on ThemePage {
      title
      components {
        __typename
        ... on PaginatedTileList {
          title
          componentId
          paginatedItems(first: 1) {
            edges { node { __typename } }
          }
        }
      }
    }
  }
}
"""

_QUERY_COMPONENT = """
query Component($componentId: ID!, $first: Int!, $after: ID) {
  component(id: $componentId) {
    __typename
    ... on PaginatedTileList {
      title
      paginatedItems(first: $first, after: $after) {
        pageInfo { endCursor hasNextPage }
        edges {
          node {
TILE_FIELDS
          }
        }
      }
    }
  }
}
""".replace("TILE_FIELDS", _TILE_FIELDS)

# Episode lists live under `components -> ContainerNavigation -> items (tabs)`.
# Single-season programs put a PaginatedTileList directly under a tab; multi-season
# podcasts nest another ContainerNavigation (the season selector) one level deeper.
_SEASON_LIST_FIELDS = """
                title
                componentId
                paginatedItems(first: 1) {
                  edges { node { __typename } }
                }
"""

_PROGRAM_PAGE_FIELDS = """
      title
      brand
      header {
        __typename
        ... on PageHeader {
          richDescription { text }
          image { templateUrl }
          secondaryMeta { value }
        }
      }
      components {
        __typename
        ... on ContainerNavigation {
          items {
            title
            components {
              __typename
              ... on PresentersList { presenters { title type } }
              ... on PaginatedTileList {
SEASON_LIST_FIELDS
              }
              ... on ContainerNavigation {
                items {
                  title
                  components {
                    __typename
                    ... on PaginatedTileList {
SEASON_LIST_FIELDS
                    }
                  }
                }
              }
            }
          }
        }
      }
""".replace("SEASON_LIST_FIELDS", _SEASON_LIST_FIELDS)

_QUERY_PROGRAM = """
query ProgramPage($pageId: ID!) {
  page(id: $pageId) {
    __typename
    ... on RadioProgramPage {
PROGRAM_FIELDS
    }
    ... on PodcastProgramPage {
PROGRAM_FIELDS
    }
  }
}
""".replace("PROGRAM_FIELDS", _PROGRAM_PAGE_FIELDS)

_EPISODE_PAGE_FIELDS = """
      title
      header {
        __typename
        ... on IPageHeader {
          richDescription { text }
          image { templateUrl }
          primaryMeta { value }
        }
      }
      player {
        __typename
        ... on MediaPlayer {
          title
          subtitle
          image { templateUrl }
        }
      }
"""

_QUERY_EPISODE = """
query EpisodePage($pageId: ID!) {
  page(id: $pageId) {
    __typename
    ... on RadioEpisodePage {
EPISODE_FIELDS
    }
    ... on PodcastEpisodePage {
EPISODE_FIELDS
    }
  }
}
""".replace("EPISODE_FIELDS", _EPISODE_PAGE_FIELDS)

_STREAM_PLAYER_FIELDS = """
      player {
        modes {
          __typename
          ... on AudioPlayerMode {
            streamId
            durationInSeconds
          }
        }
      }
"""

_QUERY_STREAM = """
query EpisodeStream($pageId: ID!) {
  page(id: $pageId) {
    __typename
    ... on RadioEpisodePage {
STREAM_PLAYER_FIELDS
    }
    ... on PodcastEpisodePage {
STREAM_PLAYER_FIELDS
    }
  }
}
""".replace("STREAM_PLAYER_FIELDS", _STREAM_PLAYER_FIELDS)


_QUERY_FAVOURITE_ACTION = """
query FavouriteAction($pageId: ID!) {
  page(id: $pageId) {
    __typename
    ... on IPage {
      header {
        __typename
        ... on IPageHeader {
          actionItems {
            action {
              __typename
              ... on FavoriteAction { id favorite }
            }
          }
        }
      }
    }
  }
}
"""

_MUTATION_SET_FAVOURITE = """
mutation setFavorite($input: FavoriteActionInput!) {
  setFavorite(input: $input) {
    actionItem {
      action {
        __typename
        ... on FavoriteAction { id favorite }
      }
    }
  }
}
"""

_QUERY_FAVOURITES = """
query FavoritesPage($pageId: ID!) {
  page(id: $pageId) {
    __typename
    ... on FavoritesPage {
      components {
        __typename
        ... on ContainerNavigation {
          items {
            title
            components {
              __typename
              ... on PaginatedTileList {
                componentId
                paginatedItems(first: 100) {
                  pageInfo { endCursor hasNextPage }
                  edges {
                    node {
                      __typename
                      ... on ITile {
                        action { __typename ... on LinkAction { link } }
                      }
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""


_RESUME_PLAYER_FIELDS = """
      player {
        progress { progressInSeconds durationInSeconds completed }
        modes {
          __typename
          ... on AudioPlayerMode {
            durationInSeconds
            resumePointTemplate { mediaId mediaName }
          }
        }
      }
"""

_QUERY_RESUME = """
query EpisodeResume($pageId: ID!) {
  page(id: $pageId) {
    __typename
    ... on RadioEpisodePage {
RESUME_PLAYER_FIELDS
    }
    ... on PodcastEpisodePage {
RESUME_PLAYER_FIELDS
    }
  }
}
""".replace("RESUME_PLAYER_FIELDS", _RESUME_PLAYER_FIELDS)


_EPISODE_MENU_FIELDS = """
      player {
        modes {
          __typename
          ... on AudioPlayerMode { broadcastStartDate }
        }
      }
      menu { items { title componentId } }
"""

_QUERY_EPISODE_MENU = """
query EpisodeMenu($pageId: ID!) {
  page(id: $pageId) {
    __typename
    ... on RadioEpisodePage {
EPISODE_MENU_FIELDS
    }
    ... on PodcastEpisodePage {
EPISODE_MENU_FIELDS
    }
  }
}
""".replace("EPISODE_MENU_FIELDS", _EPISODE_MENU_FIELDS)

# The playlist menu id resolves to a ContainerNavigationItem wrapping the actual
# song PaginatedTileList; this query digs out that inner list's componentId.
_QUERY_PLAYLIST_TAB = """
query PlaylistTab($componentId: ID!) {
  component(id: $componentId) {
    __typename
    ... on ContainerNavigationItem {
      components {
        __typename
        ... on PaginatedTileList { componentId tileContentType }
      }
    }
  }
}
"""
