"""Helper to provide the GraphQL Queries."""

from gql import gql

organizations_query = gql("""
query Organizations {
  organizations {
    nodes {
      coreId
      name
      title
      images {
        nodes {
          title
          imageId
          url
          width
          aspectRatio
        }
      }
    publicationServicesByOrganizationName {
      nodes {
          coreId
            }
        }
    }
  }
}
""")

publications_query = gql("""
query PublicationServices ($coreId: String!) {
  organizationByCoreId(coreId: $coreId) {
    publicationServicesByOrganizationName {
      nodes {
          coreId
          title
          synopsis
          imagesList {
            title
            url
            width
            aspectRatio
          }

      }
    }
  }
}
""")

publications_list_query = gql("""
query Publications($coreId: String!) {
  publicationServiceByCoreId(coreId: $coreId) {
    title
    permanentLivestreams {
      nodes {
        current
        title
        audioList {
          audioBitrate
          href
          audioCodec
          availableFrom
          availableTo
        }
        coreId
        summary
        imagesList {
          url
          width
          aspectRatio
          title
        }
      }
    }
    genre
    synopsis
    socialMediaAccounts {
      url
      service
    }
    shows {
      nodes {
        coreId
        title
        synopsis
        imagesList {
          url
          width
          aspectRatio
          title
        }
        editorialCategoriesList {
          title
        }
      }
    }
  }
}
""")


radio_metadata_query = gql("""
query PublicationServiceMetadata($coreId: String!) {
  publicationServiceByCoreId(coreId: $coreId) {
    genre
    imagesList {
      url
      width
      title
    }
    socialMediaAccounts {
      url
      service
    }
    homepageUrl
    synopsis
  }
}
""")

livestream_query = gql("""
query Livestream($coreId: String!) {
  permanentLivestreamByCoreId(coreId: $coreId) {
    publisherCoreId
    summary
    current
    title
    imagesList {
      title
      url
      width
      aspectRatio
    }
    audioList {
        audioBitrate
        href
        audioCodec
        availableFrom
        availableTo
    }
  }
}
""")

show_query = gql("""
query Show($showId: ID!) {
  show(id: $showId) {
    synopsis
    title
    imagesList {
      width
      url
      aspectRatio
    }
    publicationService {
      title
    }
    items {
      totalCount
      nodes {
        duration
        audioList {
          audioBitrate
          audioCodec
          availableFrom
          availableTo
          href
        }
        title
        titleClean
        titleWithoutNumber
        episodeNumber
        imagesList {
          title
          url
          width
          aspectRatio
        }
        coreId
        synopsis
        summary
      }
    }
    showType
    editorialCategoriesList {
      title
    }
  }
}
""")

show_episode_query = gql("""
query ShowEpisode($coreId: String!) {
  itemByCoreId(coreId: $coreId) {
      duration
      audioList {
        audioBitrate
        audioCodec
        availableFrom
        availableTo
        href
      }
      title
      titleClean
      titleWithoutNumber
      episodeNumber
      imagesList {
        title
        url
        width
        aspectRatio
      }
      coreId
    showId
    rowId
    synopsis
    summary
  }
}
""")


ard_search_query = gql("""
query Search($query: String) {
  search(query: $query) {
    shows {
      totalCount
      title
      nodes {
        coreId
        title
        synopsis
        imagesList {
          title
          url
          width
          aspectRatio
        }
      }
    }
  }
}
""")


query = gql("""
query Nodes {
  organizations {
    nodes {
      name
      title
      publicationServicesByOrganizationName {
        nodes {
          title
          genre
          permanentLivestreams {
            nodes {
              audioList {
                href
                audioCodec
                distributionType
                audioBitrate
                audioChannel
              }
              description
            }
          }
          description
          socialMediaAccounts {
            service
            url
          }
          homepageUrl
          synopsis
          image {
            url
            description
            attribution
            url1X1
          }
          nodeId
        }
      }
      _links
      images {
        nodes {
          title
          imageId
          url
        }
      }
    }
  }
}
""")
