"""Helper to provide the GraphQL Queries."""

from gql import gql

image_list = """
imagesList {
  title
  url
  width
  aspectRatio
}
"""


audio_list = """
audioList {
  audioBitrate
  href
  audioCodec
  availableFrom
  availableTo
}
"""


publication_service_metadata = """
    title
    genre
    synopsis
    homepageUrl
    socialMediaAccounts {
      url
      service
    }
"""


organizations_query = gql(
    """
query Organizations {
  organizations {
    nodes {
      coreId
      name
      title
      publicationServicesByOrganizationName {
        nodes {
          coreId
          title"""
    + image_list
    + """
        }
      }
    }
  }
}
"""
)


publication_services_query = gql(
    """
query PublicationServices ($coreId: String!) {
  organizationByCoreId(coreId: $coreId) {
    publicationServicesByOrganizationName {
      nodes {
          coreId
          title
          synopsis"""
    + image_list
    + """
      }
    }
  }
}
"""
)


publications_list_query = gql(
    """
query Publications($coreId: String!) {
  publicationServiceByCoreId(coreId: $coreId) {
    permanentLivestreams {
      nodes {
        title
        coreId
        publicationService {"""
    + publication_service_metadata
    + """
        }"""
    + image_list
    + """
      }
    }
    shows {
      nodes {
        coreId
        title
        synopsis
        items {
          totalCount
        }
        publicationService {"""
    + publication_service_metadata
    + """
        }
        editorialCategoriesList {
          title
        }"""
    + image_list
    + """
      }
    }
  }
}
"""
)


livestream_query = gql(
    """
query Livestream($coreId: String!) {
  permanentLivestreamByCoreId(coreId: $coreId) {
    publisherCoreId
    summary
    current
    title
    publicationService {"""
    + publication_service_metadata
    + """
    }"""
    + image_list
    + audio_list
    + """
  }
}
"""
)


show_length_query = gql("""
query Show($showId: ID!, $filter: ItemFilter) {
  show(id: $showId) {
    items(filter: $filter) {
      totalCount
    }
  }
}
""")


show_query = gql(
    """
query Show($showId: ID!, $first: Int, $offset: Int, $filter: ItemFilter) {
  show(id: $showId) {
    synopsis
    title
    showType
    items(first: $first, offset: $offset, filter: $filter) {
      totalCount
      nodes {
        duration
        title
        status
        episodeNumber
        coreId
        summary"""
    + audio_list
    + image_list
    + """
      }
    }
    editorialCategoriesList {
      title
    }
    publicationService {"""
    + publication_service_metadata
    + """
    }"""
    + image_list
    + """
  }
}
"""
)


show_episode_query = gql(
    """
query ShowEpisode($coreId: String!) {
  itemByCoreId(coreId: $coreId) {
    show {
      title
    }
    duration
    title
    episodeNumber
    coreId
    showId
    rowId
    synopsis
    summary"""
    + audio_list
    + image_list
    + """
  }
}
"""
)


search_shows_query = gql(
    """
query Search($query: String, $limit: Int) {
  search(query: $query, limit: $limit) {
    shows {
      totalCount
      title
      nodes {
        synopsis
        title
        coreId"""
    + image_list
    + """
        publicationService {"""
    + publication_service_metadata
    + """
    }
        items {
          totalCount
        }
        showType
        editorialCategoriesList {
          title
        }
      }
    }
  }
}
"""
)


search_radios_query = gql(
    """
query RadioSearch($filter: PermanentLivestreamFilter, $first: Int) {
  permanentLivestreams(filter: $filter, first: $first) {
    nodes {
      coreId
      title"""
    + image_list
    + """
        publicationService {"""
    + publication_service_metadata
    + """
      }
    }
  }
}
"""
)


check_login_query = gql(
    """
query CheckLogin($loginId: String!) {
  allEndUsers(filter: { loginId: { eq: $loginId } }) {
    count
    nodes {
      id
      syncSuccessful
    }
  }
}
"""
)


get_subscriptions_query = gql(
    """
query GetBookmarksByLoginId($loginId: String!) {
  allEndUsers(filter: { loginId: { eq: $loginId } }) {
    count
    nodes {
      subscriptions {
        programSets {
          nodes {
            subscribedProgramSet {
              coreId
            }
          }
        }
      }
    }
  }
}
"""
)


get_history_query = gql(
    """
query GetBookmarksByLoginId($loginId: String!, $count: Int = 96) {
  allEndUsers(filter: { loginId: { eq: $loginId } }) {
    count
    nodes {
      history(first: $count, orderBy: LASTLISTENEDAT_DESC) {
        nodes {
          progress
          item {
            coreId
            duration
          }
        }
      }
    }
  }
}
"""
)

update_history_entry = gql(
    """
mutation AddHistoryEntry(
  $itemId: ID!
  $progress: Float!
) {
  upsertHistoryEntry(
    input: {
      item: { id: $itemId }
      progress: $progress
    }
  ) {
    changedHistoryEntry {
      id
      progress
      lastListenedAt
    }
  }
}"""
)
